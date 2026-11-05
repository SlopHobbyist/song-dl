"""Jobs the browser starts with a POST and then polls: searching for a song,
scanning a playlist, and fetching a file to hand to the visitor.

A search or scan registers an Entry for every file it finds: the ranked list of
copies of one song in one format. The browser downloads an Entry, or an admin
queues it on the server (see server_queue.py), by naming its id.
"""

import asyncio
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from . import matching
from .engine import LOW_DISK, UNREACHABLE, Engine, mb
from .errors import UserError
from .playlist import Track
from .slskd import SlskdError

log = logging.getLogger('songdl')

JOB_TTL = 3600                # seconds a finished job is kept
ENTRY_TTL = 2 * 3600          # seconds a found file can still be downloaded after it was last used
SCAN_IDLE = 120               # a running scan nobody has polled for this long is abandoned
DOWNLOAD_IDLE = 180           # ...and a running download. Long enough for a background tab, which a
                              # browser may let poll only once a minute
SEARCH_SLOTS = 4              # playlist searches running at once, across all users
MAX_SCANS = 3                 # playlists scanned at once
MAX_DOWNLOADS_PER_IP = 8      # files one visitor can be fetching at once
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def file_stem(title, artist=''):
    """"Artist - Title", safe to use as a file name."""
    label = f'{artist} - {title}' if artist else title
    return ILLEGAL.sub('_', label).strip(' .')[:150] or 'song'


class Entry:
    """A song in one format that was found: the copies to try, best first."""

    def __init__(self, cands, fmt, title, artist, album=''):
        self.id = secrets.token_urlsafe(12)
        self.cands = cands
        self.fmt = fmt
        self.title = title
        self.artist = artist
        self.album = album or ''
        self.used = time.monotonic()

    @property
    def best(self):
        return self.cands[0]

    def touch(self):
        self.used = time.monotonic()

    def view(self):
        c = self.best
        warning = 'May be a different version of the song' if c.relevance < 2 else None
        return {
            'id': self.id,
            'format': matching.LABELS[self.fmt],
            'found': True,
            'title': self.title,
            'artist': self.artist,
            'album': self.album,
            'duration': c.length,
            'quality': c.quality_label,
            'size': c.size,
            'filename': f'{file_stem(self.title, self.artist)}{c.ext}',
            'warning': warning,
        }


class Job:
    def __init__(self):
        self.id = secrets.token_urlsafe(12)
        self.state = 'running'     # running, done, failed
        self.error = None
        self.updated = time.monotonic()

    def fail(self, message):
        self.state = 'failed'
        self.error = message

    def finish(self):
        self.state = 'done'

    async def guard(self, coro):
        """Runs the job body, turning exceptions into a failed state."""
        try:
            await coro
        except UserError as e:
            self.fail(str(e))
        except SlskdError as e:
            log.warning('slskd error in %s: %s', type(self).__name__, e)
            self.fail(UNREACHABLE if e.status is None else 'Something went wrong talking to Soulseek. Please try again.')
        except asyncio.CancelledError:
            if self.state == 'running':
                self.fail('Cancelled')
            raise
        except Exception:
            log.exception('%s crashed', type(self).__name__)
            self.fail('Something went wrong on the server. Please try again.')
        finally:
            self.updated = time.monotonic()


class SearchJob(Job):
    """Looks for one song in every format asked for."""

    def __init__(self, query, formats):
        super().__init__()
        self.query = query
        self.formats = formats
        self.status = 'Searching Soulseek'
        self.found = {}     # format -> Entry
        self.missing = []   # formats nothing was found in

    def view(self):
        results = [self.found[f].view() for f in self.formats if f in self.found]
        first = results[0] if results else {}
        return {
            'state': self.state,
            'status': self.status,
            'error': self.error,
            'query': self.query,
            'title': first.get('title'),
            'artist': first.get('artist'),
            'album': first.get('album'),
            'results': results,
            'missing': [matching.LABELS[f] for f in self.formats if f in self.missing],
        }


class ScanTrack:
    """One song of a scanned playlist. state: pending, searching, found, missing."""

    def __init__(self, track: Track):
        self.track = track
        self.state = 'pending'
        self.found = {}      # format -> Entry

    def view(self, formats):
        results = [self.found[f].view() for f in formats if f in self.found]
        return {
            'title': self.track.title,
            'artist': self.track.artist,
            'state': self.state,
            'results': results,
            'size': sum(r['size'] for r in results) or None,
        }


class ScanJob(Job):
    """Looks for every song of a playlist, in every format asked for."""

    def __init__(self, name, formats, tracks, skipped):
        super().__init__()
        self.name = name
        self.formats = formats
        self.tracks = [ScanTrack(t) for t in tracks]
        self.skipped = skipped
        self.polled = time.monotonic()
        self.tasks = []

    def view(self):
        self.polled = self.updated = time.monotonic()
        return {
            'state': self.state,
            'error': self.error,
            'name': self.name,
            'formats': [matching.LABELS[f] for f in self.formats],
            'skipped': self.skipped,
            'total': len(self.tracks),
            'scanned': sum(t.state in ('found', 'missing') for t in self.tracks),
            'tracks': [t.view(self.formats) for t in self.tracks],
        }

    def cancel(self):
        if self.state == 'running':
            self.fail('Cancelled')
        for task in self.tasks:
            task.cancel()


class DownloadJob(Job):
    """Fetches one Entry onto the server so the browser can download it."""

    def __init__(self, entry: Entry, ip):
        super().__init__()
        self.entry = entry
        self.ip = ip
        self.progress = None
        self.speed = None
        self.note = 'Starting'
        self.file = None    # URL of the finished file
        self.name = None    # file name to give the visitor
        self.path = None
        self.polled = time.monotonic()
        self.task = None

    def seen(self):
        self.polled = self.updated = time.monotonic()
        self.entry.touch()

    def abandon(self):
        """Stops a download nobody is waiting for. Only the visitor's page knows the job, so once it
        stops asking, nobody could ever get the file. The copy from Soulseek is cancelled too, unless
        something else (the server queue, another visitor) wants the same file."""
        if self.state == 'running':
            self.fail('Stopped because the page that asked for it was closed.')
        if self.task:
            self.task.cancel()

    def view(self):
        return {
            'state': self.state,
            'error': self.error,
            'progress': self.progress,
            'speed': self.speed,
            'note': self.note,
            'file': self.file,
            'name': self.name,
            'size': self.entry.best.size,
            'title': self.entry.title,
            'format': matching.LABELS[self.entry.fmt],
        }


class Manager:
    def __init__(self, cfg, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        self.searches = {}
        self.scans = {}
        self.downloads = {}
        self.entries = {}
        self.search_slots = asyncio.Semaphore(SEARCH_SLOTS)

    def entry(self, entry_id):
        entry = self.entries.get(str(entry_id))
        if not entry:
            raise UserError('That result has expired. Please search again.', 404)
        entry.touch()
        return entry

    def _register(self, cands, fmt, title, artist, album=''):
        entry = Entry(cands, fmt, title, artist, album)
        self.entries[entry.id] = entry
        return entry

    # Search

    async def start_search(self, query, formats):
        await self.engine.check_connected()
        job = SearchJob(query, formats)
        self.searches[job.id] = job
        self.engine.spawn(job.guard(self._search(job)))
        return job

    async def _search(self, job: SearchJob):
        def status(text):
            job.status = text

        rankings = await asyncio.gather(*(self.engine.search(job.query, f, status) for f in job.formats))
        for fmt, ranking in zip(job.formats, rankings):
            if not ranking.candidates:
                job.missing.append(fmt)
                continue
            best = ranking.candidates[0]
            meta = best.meta or {}
            job.found[fmt] = self._register(
                ranking.candidates, fmt, meta.get('title') or Path(best.basename).stem,
                meta.get('artist') or '', meta.get('album') or '')
        job.status = 'Done'
        job.finish()

    # Playlist scan

    async def start_scan(self, name, formats, tracks, skipped=0):
        await self.engine.check_connected()
        if sum(j.state == 'running' for j in self.scans.values()) >= MAX_SCANS:
            raise UserError('Song-DL is busy scanning other playlists right now. Please try again in a few minutes.', 503)
        job = ScanJob(name, formats, tracks, skipped)
        self.scans[job.id] = job
        job.tasks = [self.engine.spawn(self._scan_track(job, t)) for t in job.tracks]
        self.engine.spawn(job.guard(self._scan(job)))
        return job

    async def _scan(self, job: ScanJob):
        await asyncio.gather(*job.tasks, return_exceptions=True)
        if job.state == 'running':
            job.finish()

    async def _scan_track(self, job: ScanJob, st: ScanTrack):
        async def one(fmt):
            async with self.search_slots:
                st.state = 'searching'
                try:
                    cands = await self.engine.find_track(st.track, fmt)
                except UserError as e:
                    if e.status != 400:   # 400 is "No match found"
                        log.warning('scan of %r stopped: %s', st.track.label, e)
                    return
                except SlskdError as e:
                    log.warning('slskd error scanning %r: %s', st.track.label, e)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('scan of %r crashed', st.track.label)
                    return
                st.found[fmt] = self._register(cands, fmt, st.track.title, st.track.artist, st.track.album)

        await asyncio.gather(*(one(f) for f in job.formats))
        st.state = 'found' if st.found else 'missing'
        job.updated = time.monotonic()

    # Fetching for the visitor

    async def start_download(self, entry_id, ip):
        entry = self.entry(entry_id)
        if sum(j.state == 'running' and j.ip == ip for j in self.downloads.values()) >= MAX_DOWNLOADS_PER_IP:
            raise UserError('You already have a lot of downloads going. Please wait for some to finish.', 429)
        await self.engine.check_connected()
        if self.engine.low_disk():
            raise UserError(LOW_DISK, 507)
        job = DownloadJob(entry, ip)
        self.downloads[job.id] = job
        job.task = self.engine.spawn(job.guard(self._download(job)))
        return job

    async def _download(self, job: DownloadJob):
        def update(progress, speed, note):
            job.progress, job.speed, job.note = progress, speed, note
            job.entry.touch()

        fetch = await self.engine.download(job.entry.cands, update)
        job.path = fetch.path
        job.name = file_stem(job.entry.title, job.entry.artist) + fetch.path.suffix.lower()
        job.file = f'/files/{fetch.id}/{quote(fetch.path.name)}?as={quote(job.name)}'
        job.progress, job.speed, job.note = 1, None, f'Ready ({mb(fetch.cand.size)})'
        job.finish()

    # Housekeeping

    def prune(self):
        now = time.monotonic()
        for jobs in (self.searches, self.scans, self.downloads):
            for jid in [j for j, job in jobs.items() if job.state != 'running' and now - job.updated > JOB_TTL]:
                del jobs[jid]
        for job in self.scans.values():
            if job.state == 'running' and now - job.polled > SCAN_IDLE:
                log.info('scan %s abandoned', job.id)
                job.cancel()
        for job in self.downloads.values():
            if job.state == 'running' and now - job.polled > DOWNLOAD_IDLE:
                log.info('download %s of %r abandoned', job.id, job.entry.title)
                job.abandon()
        # Found files stay for a while after last use; a queue item keeps its own copy of the list
        for eid in [e for e, entry in self.entries.items() if now - entry.used > ENTRY_TTL]:
            del self.entries[eid]
        self.engine.prune()

    def shutdown(self):
        self.engine.shutdown()
