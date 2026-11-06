"""Playlist imports: an admin's whole playlist, searched song by song in the
background, each song put in the server queue as soon as it is found.

A browser scan (jobs.ScanJob) only lives while its page is open and is meant
for a few hundred songs. An import is for thousands: it doesn't need a page
open, it is written to disk and carries on after a restart, and the songs it
finds go straight into the server queue, so nothing it found can expire.

Each song is looked for in the sources the admin ticked, in the order they put
them in, and stops at the first that has it: Soulseek in the first of the
admin's formats it has (tried in the order they put them in), or the best
audio on SoundCloud or YouTube (see soundcloud_audio.py,
youtube_audio.py).

It is slow on purpose. Soulseek bans clients that search too fast, so every
search waits its turn in the engine's SearchLimiter, behind visitors' searches
and scans. Expect about 9 searches a minute: one per song and format tried
(a song found in the first format needs just one), and a second one when a
search for the artist comes back empty.

States: running, paused, done, cancelled. Songs: pending, searching, found, missing.
"""

import asyncio
import csv
import io
import json
import logging
import os
import secrets
import time

from . import library, matching
from .engine import IMPORT, SEARCH_WINDOW, SEARCHES_PER_WINDOW, Engine
from .errors import UserError
from .jobs import Entry
from .matching import SOURCE_LABELS, more_after
from .playlist import Track
from .server_queue import ServerQueue, normalize_folder
from .slskd import SlskdError

log = logging.getLogger('songdl')

MAX_TRACKS = 10_000      # Spotify's own limit on a playlist
MAX_IMPORTS = 50         # kept in the list, finished ones included
WORKERS = 3              # songs searched at once; the search limit sets the real pace
RETRY_SECONDS = 30       # wait after Soulseek or slskd goes away
TICK_SECONDS = 2
SEARCHES_PER_SONG = 1.15  # one per format, plus the odd second search for the title alone


class ImportTrack:
    __slots__ = ('track', 'state', 'found', 'note')

    def __init__(self, track: Track, state='pending', found=(), note=''):
        self.track = track
        self.state = state
        self.found = list(found)   # the format (or 'soundcloud' / 'youtube') queued; at most one
        self.note = note

    def to_list(self):
        t = self.track
        return [t.title, t.artist, t.album, t.duration, self.state, self.found, self.note]

    @classmethod
    def from_list(cls, row):
        title, artist, album, duration, state, found, note = row
        if state == 'searching':
            state = 'pending'
        return cls(Track(title, artist, album, duration), state, found, note)


class Import:
    def __init__(self, name, formats, folder, by, tracks, skipped=0, sources=('soulseek',)):
        self.id = secrets.token_urlsafe(9)
        self.name = name
        self.formats = formats
        self.sources = list(sources)
        self.folder = folder
        self.by = by
        self.tracks = tracks           # [ImportTrack]
        self.skipped = skipped         # CSV rows without a song name
        self.state = 'running'
        self.note = ''
        self.created = int(time.time() * 1000)
        self.finished = None
        self.next = 0                  # index of the first song that might still be pending
        # This run's pace, for the time left
        self.run_started = None
        self.run_done = 0

    def counts(self):
        c = {'pending': 0, 'searching': 0, 'found': 0, 'missing': 0}
        for t in self.tracks:
            c[t.state] += 1
        return c

    def take(self):
        """The next pending song, marked as being searched, or None."""
        while self.next < len(self.tracks):
            t = self.tracks[self.next]
            self.next += 1
            if t.state == 'pending':
                t.state = 'searching'
                return t
        return None

    def eta(self, left):
        if not left or self.state != 'running':
            return None
        if self.run_done >= 5 and self.run_started:
            per_song = (time.monotonic() - self.run_started) / self.run_done
        else:
            # Most songs are found in the first format or two; the ones never found try them all
            tries = (1 + len(self.formats)) / 2
            per_song = tries * SEARCHES_PER_SONG * SEARCH_WINDOW / SEARCHES_PER_WINDOW
        return round(left * per_song)

    def view(self):
        c = self.counts()
        left = c['pending'] + c['searching']
        return {
            'id': self.id,
            'name': self.name,
            'formats': [f.upper() for f in self.formats] if 'soulseek' in self.sources else [],
            'sources': [SOURCE_LABELS[s] for s in self.sources],
            'folder': self.folder + '/' if self.folder else '',
            'by': self.by,
            'state': self.state,
            'note': self.note,
            'total': len(self.tracks),
            'found': c['found'],
            'missing': c['missing'],
            'left': left,
            'skipped': self.skipped,
            'queued': sum(len(t.found) for t in self.tracks),
            # Found, but not on Soulseek: they're in the missing CSV too
            'elsewhere': sum(bool(t.found) and not any(f in self.formats for f in t.found) for t in self.tracks)
            if 'soulseek' in self.sources else 0,
            'current': [t.track.label for t in self.tracks if t.state == 'searching'][:WORKERS],
            'eta': self.eta(left),
            'created': self.created,
            'finished': self.finished,
        }

    def missing_csv(self):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator='\r\n')
        writer.writerow(['Track Name', 'Artist Name(s)', 'Album Name', 'Duration (ms)', 'Not found in'])
        for t in self.tracks:
            # Not on Soulseek in any of the formats (found on SoundCloud or YouTube, maybe)
            if t.state in ('found', 'missing') and not any(f in self.formats for f in t.found):
                d = t.track.duration
                writer.writerow([t.track.title, t.track.artist, t.track.album, d * 1000 if d else '',
                                 ' '.join(f.upper() for f in self.formats)])
        return '﻿' + out.getvalue()

    def to_dict(self):
        return {'id': self.id, 'name': self.name, 'formats': self.formats, 'sources': self.sources,
                'folder': self.folder, 'by': self.by,
                'state': self.state, 'note': self.note, 'created': self.created, 'finished': self.finished,
                'skipped': self.skipped, 'tracks': [t.to_list() for t in self.tracks]}

    @classmethod
    def from_dict(cls, d):
        imp = cls(d['name'], d['formats'], d['folder'], d['by'],
                  [ImportTrack.from_list(r) for r in d['tracks']], d.get('skipped', 0),
                  d.get('sources') or ('soulseek',))
        imp.id, imp.state, imp.note = d['id'], d['state'], d.get('note') or ''
        imp.created, imp.finished = d['created'], d.get('finished')
        return imp


class Importer:
    def __init__(self, cfg, engine: Engine, queue: ServerQueue):
        self.cfg = cfg
        self.engine = engine
        self.queue = queue
        self.imports = {}          # id -> Import, oldest first
        self.tasks = {}            # id -> {asyncio tasks searching its songs}
        self.wake = asyncio.Event()
        self.dirty = False
        self.saved_text = None
        self.offline_until = 0

    # Adding and controlling

    def add(self, name, formats, folder, by, tracks, skipped=0, sources=('soulseek',)):
        folder = normalize_folder(folder)
        self.queue.dest_dir(folder)   # refuses anything that would leave the downloads folder
        if len(tracks) > MAX_TRACKS:
            raise UserError(f'That playlist has {len(tracks)} songs. An import can have up to {MAX_TRACKS}.')
        if sum(i.state in ('running', 'paused') for i in self.imports.values()) >= MAX_IMPORTS:
            raise UserError('There are too many imports going. Let some finish or delete them first.', 409)
        imp = Import(name, formats, folder, by, [ImportTrack(t) for t in tracks], skipped, sources)
        self.imports[imp.id] = imp
        self._trim()
        self._changed()
        log.info('%s imported %r: %d songs into %r (%s; %s)', by, name, len(tracks), folder, ','.join(formats),
                 ','.join(sources))
        return imp

    def get(self, import_id):
        imp = self.imports.get(import_id)
        if not imp:
            raise UserError('That import no longer exists.', 404)
        return imp

    def pause(self, imp):
        if imp.state == 'running':
            imp.state, imp.note = 'paused', ''
            self._interrupt(imp)
            self._changed()

    def resume(self, imp):
        if imp.state in ('paused', 'cancelled'):
            imp.state, imp.note, imp.finished, imp.next = 'running', '', None, 0
            imp.run_started, imp.run_done = None, 0
            self._changed()

    def cancel(self, imp):
        if imp.state in ('running', 'paused'):
            imp.state, imp.note = 'cancelled', ''
            imp.finished = int(time.time() * 1000)
            self._interrupt(imp)
            self._changed()

    def delete(self, imp):
        """Forgets the import. Songs it already queued stay in the queue."""
        self._interrupt(imp)
        self.imports.pop(imp.id, None)
        self._changed()

    def _interrupt(self, imp):
        for task in list(self.tasks.get(imp.id, ())):
            task.cancel()
        for t in imp.tracks:
            if t.state == 'searching':
                t.state = 'pending'
        imp.next = 0

    def _trim(self):
        ended = [i for i in self.imports.values() if i.state in ('done', 'cancelled')]
        for imp in ended[:max(0, len(self.imports) - MAX_IMPORTS)]:
            del self.imports[imp.id]

    def view(self):
        return {'imports': [i.view() for i in reversed(self.imports.values())]}

    def _changed(self):
        self.dirty = True
        self.wake.set()

    # Running

    async def run(self):
        """Keeps WORKERS songs being searched, and the imports file current."""
        while True:
            try:
                await asyncio.wait_for(self.wake.wait(), TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                await self._dispatch()
                self.save()
            except Exception:
                log.exception('import loop failed')

    def _busy(self):
        return sum(len(tasks) for tasks in self.tasks.values())

    async def _dispatch(self):
        for imp in self.imports.values():
            if imp.state == 'running' and not any(t.state in ('pending', 'searching') for t in imp.tracks):
                imp.state, imp.finished = 'done', int(time.time() * 1000)
                log.info('import %r finished', imp.name)
                self.dirty = True
        if self._busy() >= WORKERS or time.monotonic() < self.offline_until:
            return
        running = [i for i in self.imports.values() if i.state == 'running']
        if not running:
            return
        # Soulseek being away only holds up imports that search it
        if any('soulseek' in i.sources for i in running) and not await self.engine.connected():
            self.offline_until = time.monotonic() + RETRY_SECONDS
            running = [i for i in running if 'soulseek' not in i.sources]
            if not running:
                return
        # Oldest import first; a newer one gets the slots the older one can't use
        for imp in running:
            # (It may have been paused while the connection was checked)
            while imp.state == 'running' and self._busy() < WORKERS:
                t = imp.take()
                if not t:
                    break
                if imp.run_started is None:
                    imp.run_started = time.monotonic()
                task = self.engine.spawn(self._search(imp, t))
                self.tasks.setdefault(imp.id, set()).add(task)
                task.add_done_callback(lambda task, i=imp.id: self._ended(i, task))
                self.dirty = True

    def _ended(self, import_id, task):
        tasks = self.tasks.get(import_id)
        if tasks:
            tasks.discard(task)
            if not tasks:
                del self.tasks[import_id]
        self.wake.set()

    async def _search(self, imp: Import, t: ImportTrack):
        try:
            if not t.found and not self._local(imp, t):
                return
            for source in imp.sources:
                if t.found:   # (an import saved by an older version may have one already)
                    break
                if source == 'soulseek':
                    ok = await self._soulseek(imp, t)
                else:
                    ok = await self._elsewhere(imp, t, source)
                if not ok:
                    return
            t.state = 'found' if t.found else 'missing'
            t.note = ''
            imp.run_done += 1
        except SlskdError as e:
            log.warning('import %r: slskd error on %r: %s', imp.name, t.track.label, e)
            self._retry_later(imp, t)
        except asyncio.CancelledError:
            if t.state == 'searching':
                t.state = 'pending'
            raise
        except Exception:
            log.exception('import search for %r crashed', t.track.label)
            t.state, t.note = 'missing', 'Something went wrong searching for this song'
        finally:
            self._changed()

    async def _soulseek(self, imp: Import, t: ImportTrack):
        """Queues the song from Soulseek in the first of the formats it has. False when the import
        had to stop."""
        for fmt in imp.formats:
            try:
                cands = await self.engine.find_track(t.track, fmt, priority=IMPORT)
            except UserError as e:
                if e.status != 400:   # 400 is "No match found"
                    log.info('import search for %r failed: %s', t.track.label, e)
                continue
            entry = Entry(cands, fmt, t.track.title, t.track.artist, t.track.album)
            try:
                self.queue.add([entry], imp.folder, imp.by)
            except UserError as e:
                # The queue is full, say: stop until someone makes room
                self._pause(imp, t, e)
                return False
            t.found.append(fmt)
            return True
        return True

    def _local(self, imp: Import, t: ImportTrack):
        """Queues the song from the files already on the server, if a copy there will do (see
        library.py), so it isn't searched for or downloaded again. False when the import had to stop."""
        order = library.steps(imp.formats, imp.sources)
        cands = self.engine.library.find(t.track.query, order, t.track.duration)
        if not cands:
            return True
        c = cands[0]
        step = c.fmt if c.fmt in imp.formats and c.origin not in matching.ELSEWHERE else c.origin
        entry = Entry(cands, c.fmt, t.track.title, t.track.artist, t.track.album)
        try:
            self.queue.add([entry], imp.folder, imp.by)
        except UserError as e:
            self._pause(imp, t, e)
            return False
        t.found.append(step)
        return True

    def _pause(self, imp, t, e):
        imp.state, imp.note = 'paused', str(e)
        t.state = 'pending'
        imp.next = 0
        log.warning('import %r paused: %s', imp.name, e)

    async def _elsewhere(self, imp: Import, t: ImportTrack, source):
        """Queues the song's best audio from SoundCloud or YouTube, if it's there. False when the
        import had to stop."""
        try:
            cands = await self.engine.find_elsewhere(source, t.track, more=more_after(source, imp.sources))
        except UserError as e:
            log.info('import: %s search for %r failed: %s', source, t.track.label, e)
            return True
        if not cands:
            return True
        entry = Entry(cands, cands[0].fmt, t.track.title, t.track.artist, t.track.album)
        try:
            self.queue.add([entry], imp.folder, imp.by)
        except UserError as e:
            self._pause(imp, t, e)
            return False
        t.found.append(source)
        return True

    def _retry_later(self, imp, t):
        t.state = 'pending'
        imp.next = 0
        self.offline_until = time.monotonic() + RETRY_SECONDS

    # Remembering imports, so they survive a restart

    def load(self):
        try:
            data = json.loads(self.cfg.imports_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning('can\'t read %s: %s', self.cfg.imports_file, e)
            return
        for d in data.get('imports', []):
            try:
                imp = Import.from_dict(d)
            except Exception as e:
                log.warning('skipped a saved import: %r', e)
                continue
            self.imports[imp.id] = imp
        running = sum(i.state == 'running' for i in self.imports.values())
        if self.imports:
            log.info('loaded %d playlist import(s), %d running', len(self.imports), running)

    def save(self):
        if not self.dirty:
            return
        text = json.dumps({'version': 1, 'imports': [i.to_dict() for i in self.imports.values()]})
        self.dirty = False
        if text == self.saved_text:
            return
        path = self.cfg.imports_file
        tmp = path.with_name(path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, path)
            self.saved_text = text
        except OSError as e:
            self.dirty = True
            log.warning('can\'t save %s: %s', path, e)

    def shutdown(self):
        for imp in self.imports.values():
            self._interrupt(imp)
        self.dirty = True
        self.save()
