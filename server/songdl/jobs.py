"""Jobs the browser starts with a POST and then polls: searching for a song,
scanning a playlist, and fetching a file to hand to the visitor.

Each search or scan first looks for the song among the files already on the
server (library.py), whatever the visitor ticked: a copy there in one of their
formats, or from one of their sources, is handed out again without searching.
Otherwise it looks in the sources the visitor ticked, in the order they put
them in, and stops at the first that has the song: Soulseek in the first of
the visitor's formats it has (they're tried in the order the visitor put them
in), SoundCloud's best stream, or YouTube's best audio. So each song is found,
and fetched, in one version only.

A search or scan registers an Entry for the file it finds: the ranked list of
copies of one song in one format. The browser downloads an Entry, or an admin
queues it on the server (see server_queue.py), by naming its id. In DJ mode the
file is made into one every CDJ plays (dj.py) before it's handed over.
"""

import asyncio
import logging
import re
import secrets
import time
from pathlib import Path
from urllib.parse import quote

from . import dj, library, matching
from .engine import LOW_DISK, SCAN, UNREACHABLE, Engine, Skip, mb
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
ELSEWHERE = matching.ELSEWHERE
ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def file_stem(title, artist=''):
    """"Artist - Title", safe to use as a file name."""
    label = f'{artist} - {title}' if artist else title
    return ILLEGAL.sub('_', label).strip(' .')[:150] or 'song'


class Entry:
    """A song in one format that was found: the copies to try, best first."""

    def __init__(self, cands, fmt, title, artist, album='', query=None, want=None):
        self.id = secrets.token_urlsafe(12)
        self.cands = cands
        self.fmt = fmt
        self.title = title
        self.artist = artist
        self.album = album or ''
        self.query = query   # what was searched for, if it was a search
        self.want = want     # a matching.Want: the playlist's length for the song, if it's a playlist's
        self.used = time.monotonic()

    @property
    def best(self):
        return self.cands[0]

    @property
    def source(self):
        return self.best.source

    def touch(self):
        self.used = time.monotonic()

    def view(self):
        c = self.best
        warning = ('A longer version than the playlist\'s' if c.fit == 'longer'
                   else 'May be a different version of the song' if c.relevance < 2 and c.fit not in ('exact', 'unknown')
                   else None)
        if c.source == 'local':
            # Says where it first came from; `local` that it's already here
            source, who, link = c.origin if c.origin in ELSEWHERE else 'soulseek', c.meta.get('from'), c.meta.get('link')
        else:
            source, who, link = c.source, c.username if c.source != 'soulseek' else None, c.url
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
            'source': source,
            'from': who,      # the channel or account
            'link': link,
            'local': c.source == 'local',
            'dj': dj.predict(c),   # what DJ mode will likely make of it
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
    """Looks for one song, in the first of the formats asked for that it can be found in."""

    def __init__(self, query, formats, sources=('soulseek',), video=None):
        super().__init__()
        self.query = query
        self.formats = formats
        self.sources = list(sources)
        self.video = video   # the YouTube video the search came from, if it came from a link
        self.status = f'Searching {matching.SOURCE_LABELS[self.sources[0]]}'
        self.found = {}     # format -> Entry; at most one
        self.missing = []   # formats nothing was found in on Soulseek, before the one that was
        self.elsewhere = None  # the Entry found on SoundCloud or YouTube, when that's where it was

    def view(self):
        results = [self.found[f].view() for f in self.formats if f in self.found]
        if self.elsewhere:
            results.append(self.elsewhere.view())
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
            'sources': [matching.SOURCE_LABELS[s] for s in self.sources],
        }


class ScanTrack:
    """One song of a scanned playlist. state: pending, searching, found, missing."""

    def __init__(self, track: Track):
        self.track = track
        self.state = 'pending'
        self.found = {}      # format, or 'soundcloud' / 'youtube' -> Entry; at most one

    def view(self, formats):
        for entry in self.found.values():
            entry.touch()   # kept while the scan is on screen, however long it took
        results = [self.found[f].view() for f in formats if f in self.found]
        results += [self.found[s].view() for s in ELSEWHERE if s in self.found]
        return {
            'title': self.track.title,
            'artist': self.track.artist,
            'album': self.track.album,
            'duration': self.track.duration,
            'state': self.state,
            'results': results,
            'size': sum(r['size'] for r in results) or None,
        }


class ScanJob(Job):
    """Looks for every song of a playlist, each in the first of the formats asked for it's found in."""

    def __init__(self, name, formats, tracks, skipped, sources=('soulseek',)):
        super().__init__()
        self.name = name
        self.formats = formats
        self.sources = list(sources)
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
            'sources': [matching.SOURCE_LABELS[s] for s in self.sources],
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

    def __init__(self, entry: Entry, ip, dj=False):
        super().__init__()
        self.entry = entry
        self.ip = ip
        self.dj = dj        # make it CDJ-ready (dj.py) once it's here
        self.size = None    # of the file handed over, once known
        self.dj_label = None
        self.progress = None
        self.speed = None
        self.note = 'Starting'
        self.file = None    # URL of the finished file
        self.name = None    # file name to give the visitor
        self.path = None
        self.polled = time.monotonic()
        self.task = None
        self.skip = Skip()

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
            'size': self.size or self.entry.best.size,
            'title': self.entry.title,
            'format': matching.LABELS[self.entry.fmt],
            'dj': self.dj_label,
            'canSkip': self.state == 'running' and self.skip.more,
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

    def _register(self, cands, fmt, title, artist, album='', query=None, want=None):
        entry = Entry(cands, fmt, title, artist, album, query, want)
        self.entries[entry.id] = entry
        return entry

    def _local(self, query, formats, sources, duration=None, track=None):
        """A copy already on the server, as (where it stands in the visitor's order: a format,
        or 'soundcloud' / 'youtube'; its Candidates), or (None, []). `track`: the playlist's
        song it has to be."""
        order = library.steps(formats, sources)
        cands = self.engine.library.find(query, order, duration, track)
        if not cands:
            return None, []
        c = cands[0]
        return (c.fmt if c.fmt in formats and c.origin not in ELSEWHERE else c.origin), cands

    # Search

    async def start_search(self, query, formats, sources=('soulseek',), video=None):
        if 'soulseek' in sources and not self.engine.library.find(query, library.steps(formats, sources)):
            await self.engine.check_connected()
        job = SearchJob(query, formats, sources, video)
        self.searches[job.id] = job
        self.engine.spawn(job.guard(self._search(job)))
        return job

    async def _search(self, job: SearchJob):
        step, cands = self._local(job.query, job.formats, job.sources)
        if cands:
            m = cands[0].meta
            entry = self._register(cands, cands[0].fmt, m.get('title') or job.query, m.get('artist') or '',
                                   m.get('album') or '', job.query)
            if step in job.formats:
                job.found[step] = entry
            else:
                job.elsewhere = entry
            job.status = 'Done'
            job.finish()
            return
        tried = []
        error = None
        for source in job.sources:
            if job.found or job.elsewhere:
                break
            label = matching.SOURCE_LABELS[source]
            job.status = f'Not on {" or ".join(tried)}. Searching {label}' if tried else f'Searching {label}'
            tried.append(label)
            if source == 'soulseek':
                await self._search_soulseek(job)
                continue
            try:
                cands = await self.engine.elsewhere[source].find(
                    job.query, video=job.video if source == 'youtube' else None, more=matching.more_after(source, job.sources))
            except UserError as e:   # couldn't search there; the next place may still have it
                error = e
                continue
            if cands:
                meta = cands[0].meta
                job.elsewhere = self._register(cands, cands[0].fmt, meta.get('title') or job.query,
                                               meta.get('artist') or '', meta.get('album') or '', job.query)
        if error and not job.found and not job.elsewhere:
            raise error
        job.status = 'Done'
        job.finish()

    async def _search_soulseek(self, job: SearchJob):
        fmt = None

        def status(text):
            # "No WAV on Soulseek. Searching Soulseek for FLAC: 12 responses so far"
            if len(job.formats) > 1:
                text = text.replace('Searching Soulseek', f'Searching Soulseek for {matching.LABELS[fmt]}')
            if job.missing:
                text = f'No {" or ".join(matching.LABELS[f] for f in job.missing)} on Soulseek. {text}'
            job.status = text

        # One format at a time, best first, so a song is only found (and fetched) once, and the
        # formats after the one found don't use up searches
        for fmt in job.formats:
            if job.missing:
                status('Searching Soulseek')
            ranking = await self.engine.search(job.query, fmt, status)
            if not ranking.candidates:
                job.missing.append(fmt)
                continue
            best = ranking.candidates[0]
            meta = best.meta or {}
            job.found[fmt] = self._register(
                ranking.candidates, fmt, meta.get('title') or Path(best.basename).stem,
                meta.get('artist') or '', meta.get('album') or '', job.query)
            return

    # Playlist scan

    async def start_scan(self, name, formats, tracks, skipped=0, sources=('soulseek',)):
        matching.mark_versions(tracks)
        if 'soulseek' in sources and not all(
                self.engine.library.find(t.query, library.steps(formats, sources), t.duration, t) for t in tracks):
            await self.engine.check_connected()
        if sum(j.state == 'running' for j in self.scans.values()) >= MAX_SCANS:
            raise UserError('Song-DL is busy scanning other playlists right now. Please try again in a few minutes.', 503)
        job = ScanJob(name, formats, tracks, skipped, sources)
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
            """Looks for the song in one format on Soulseek; True when it was found."""
            async with self.search_slots:
                st.state = 'searching'
                try:
                    cands = await self.engine.find_track(st.track, fmt, priority=SCAN)
                except UserError as e:
                    if e.status != 400:   # 400 is "No match found"
                        log.warning('scan of %r stopped: %s', st.track.label, e)
                    return False
                except SlskdError as e:
                    log.warning('slskd error scanning %r: %s', st.track.label, e)
                    return False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('scan of %r crashed', st.track.label)
                    return False
                st.found[fmt] = self._register(cands, fmt, st.track.title, st.track.artist, st.track.album, want=want)
                return True

        want = matching.Want.of(st.track)
        step, cands = self._local(st.track.query, job.formats, job.sources, st.track.duration, st.track)
        if cands:
            st.found[step] = self._register(cands, cands[0].fmt, st.track.title, st.track.artist, st.track.album,
                                            want=want)

        for source in job.sources:
            if st.found:
                break
            if source == 'soulseek':
                for fmt in job.formats:   # best first; stop at the first one found
                    if await one(fmt):
                        break
                continue
            st.state = 'searching'
            try:
                cands = await self.engine.find_elsewhere(source, st.track, more=matching.more_after(source, job.sources))
            except UserError as e:
                log.info('%s search for %r failed: %s', source, st.track.label, e)
                cands = []
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('%s search for %r crashed', source, st.track.label)
                cands = []
            if cands:
                st.found[source] = self._register(cands, cands[0].fmt, st.track.title, st.track.artist,
                                                  st.track.album, want=want)
        st.state = 'found' if st.found else 'missing'
        job.updated = time.monotonic()

    # Fetching for the visitor

    async def start_download(self, entry_id, ip, dj=False):
        entry = self.entry(entry_id)
        if sum(j.state == 'running' and j.ip == ip for j in self.downloads.values()) >= MAX_DOWNLOADS_PER_IP:
            raise UserError('You already have a lot of downloads going. Please wait for some to finish.', 429)
        if entry.source == 'soulseek':
            await self.engine.check_connected()
        if self.engine.low_disk():
            raise UserError(LOW_DISK, 507)
        job = DownloadJob(entry, ip, dj)
        self.downloads[job.id] = job
        job.task = self.engine.spawn(job.guard(self._download(job)))
        return job

    async def _download(self, job: DownloadJob):
        def update(progress, speed, note):
            job.progress, job.speed, job.note = progress, speed, note
            job.entry.touch()

        fetch = await self.engine.download(job.entry.cands, update, job.skip, job.entry.want)
        e = job.entry
        self.engine.library.add(fetch.path, e.title, e.artist, e.album, fetch.cand, e.query)
        stem = file_stem(e.title, e.artist)
        path, ready = fetch.path, ''
        if job.dj:
            job.progress, job.speed, job.note = 1, None, 'Making it CDJ-ready'
            copy = await self.engine.dj.copy(fetch.path, stem, e.title, e.artist, e.album)
            path, job.dj_label = copy.path, copy.label
            ready = copy.label + ', '
        job.path = path
        job.size = path.stat().st_size
        job.name = stem + path.suffix.lower()
        job.file = f'/files/{path.parent.name}/{quote(path.name)}?as={quote(job.name)}'
        job.progress, job.speed, job.note = 1, None, f'Ready ({ready}{mb(job.size)})'
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
