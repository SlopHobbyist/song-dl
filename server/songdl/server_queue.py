"""The server queue: songs an admin has asked to be saved on the server.

Unlike a download for the browser, a queued song doesn't depend on anyone
keeping a page open. The queue is written to disk and picked up again after a
restart. Each item is one song in one format and ends up as
<server folder>/<folder>/Artist - Title.ext, the server folder
being the downloads folder unless downloads.server_folder is set. In DJ mode
the song is made CDJ-ready first (dj.py), and a playlist's songs go in a folder
of their own, "<playlist> (DJ)", so the folder holds only files a CDJ plays.

Statuses: queued, downloading, paused, cancelled, failed, done.
"""

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from . import matching
from .engine import LOW_DISK, STAGING, Engine
from .errors import UserError
from .jobs import ILLEGAL, file_stem
from .slskd import SlskdError

log = logging.getLogger('songdl')

SLOTS = 3                 # songs downloading at once
KEPT_COPIES = 5           # copies of each song remembered in the queue file
FINISHED_KEEP_DAYS = 30   # finished items disappear from the list after this long
TICK_SECONDS = 2
MAX_ITEMS = 25_000       # a couple of big playlist imports, with room to spare
MAX_FOLDER = 120
MAX_DEPTH = 4
DJ_FOLDER = ' (DJ)'      # after a playlist's name, for its CDJ copies
DJ_EXTENSIONS = ('.wav', '.mp3')

ACTIVE = ('queued', 'downloading')
STOPPABLE = ('queued', 'downloading', 'paused')
RESTARTABLE = ('paused', 'cancelled', 'failed')


def normalize_folder(raw):
    """A folder typed by an admin, inside the downloads folder: "a/b" or "" for the downloads folder itself."""
    parts = [' '.join(p.split()) for p in str(raw or '').replace('\\', '/').split('/')]
    parts = [p for p in parts if p]
    if len('/'.join(parts)) > MAX_FOLDER:
        raise UserError(f'That folder name is too long. Keep it under {MAX_FOLDER} characters.')
    if len(parts) > MAX_DEPTH:
        raise UserError(f'That folder is nested too deep. Keep it to {MAX_DEPTH} levels.')
    for p in parts:
        if ILLEGAL.search(p):
            raise UserError('A folder name can\'t contain any of these: \\ : * ? " < > |')
        if p.startswith('.') or p.endswith('.'):
            raise UserError('A folder name can\'t start or end with a dot.')
    if parts and STAGING.match(parts[0].lower()):
        raise UserError('That folder name is reserved. Please choose another.')
    return '/'.join(parts)


def playlist_folder(name):
    """The folder a playlist's songs are saved in: its name, made safe to be one folder
    ("AC/DC: Hits?" -> "AC-DC- Hits"). Never empty, never more than one level."""
    name = ILLEGAL.sub('-', str(name or ''))
    name = ' '.join(name.split()).strip(' .')[:MAX_FOLDER].strip(' .')
    if not name or STAGING.match(name.lower()):
        name = f'Playlist {name}'.strip()
    return name


def dj_folder(name):
    """The folder for a playlist's CDJ copies: "Road Trip" -> "Road Trip (DJ)"."""
    return playlist_folder(name)[:MAX_FOLDER - len(DJ_FOLDER)].rstrip(' .') + DJ_FOLDER


class QueueItem:
    def __init__(self, entry_id, cands, title, artist, album, fmt, folder, by, dj=False):
        self.id = entry_id
        self.dj = dj   # saved as a CDJ copy (dj.py)
        self.cands = cands
        self.title, self.artist, self.album = title, artist, album
        self.format = fmt
        self.folder = folder
        self.by = by
        self.status = 'queued'
        self.progress = 0.0
        self.speed = None
        self.size = cands[0].size if cands else None
        self.note = ''
        self.created = int(time.time() * 1000)
        self.finished = None

    @property
    def label(self):
        return f'{self.artist} - {self.title}' if self.artist else self.title

    @property
    def source(self):
        return self.cands[0].source if self.cands else 'soulseek'

    def view(self):
        return {
            'id': self.id,
            'title': self.title,
            'artist': self.artist,
            'format': matching.LABELS[self.format],
            'dj': self.dj,
            'source': self.source,
            'folder': self.folder + '/' if self.folder else '',
            'by': self.by,
            'status': self.status,
            'progress': round(self.progress * 100),
            'speed': self.speed,
            'size': self.size,
            'note': self.note,
            'created': self.created,
            'finished': self.finished,
        }

    def to_dict(self):
        return {
            'id': self.id, 'title': self.title, 'artist': self.artist, 'album': self.album,
            'format': self.format, 'folder': self.folder, 'by': self.by, 'status': self.status, 'dj': self.dj,
            'size': self.size, 'note': self.note, 'created': self.created, 'finished': self.finished,
            # A finished song never needs its copies again
            'cands': [] if self.status == 'done' else [c.to_dict() for c in self.cands[:KEPT_COPIES]],
        }

    @classmethod
    def from_dict(cls, d):
        item = cls(d['id'], [matching.Candidate.from_dict(c) for c in d.get('cands') or []], d['title'], d['artist'],
                   d.get('album') or '', d['format'], d['folder'], d['by'], bool(d.get('dj')))
        item.status, item.size, item.note = d['status'], d.get('size'), d.get('note') or ''
        item.created, item.finished = d['created'], d.get('finished')
        if item.status == 'done':
            item.progress = 1.0
        return item


class ServerQueue:
    def __init__(self, cfg, engine: Engine):
        self.cfg = cfg
        self.engine = engine
        self.items = {}    # id -> QueueItem, oldest first
        self.tasks = {}    # id -> the asyncio task downloading it
        self.wake = asyncio.Event()
        self.dirty = False
        self.saved_text = None

    # Adding and controlling

    def add(self, entries, folder, by, dj=False):
        """Queues the entries (jobs.Entry) for the folder, as CDJ copies with `dj`. Returns
        (queued, skipped)."""
        folder = normalize_folder(folder)
        self.dest_dir(folder)   # refuses anything that would leave the downloads folder
        if len(self.items) + len(entries) > MAX_ITEMS:
            raise UserError('The queue is full. Clear some finished songs first.', 409)
        busy = {(i.label.lower(), i.format, i.folder, i.dj) for i in self.items.values() if i.status != 'done'}
        queued = skipped = 0
        for entry in entries:
            item = QueueItem(uuid.uuid4().hex, list(entry.cands[:KEPT_COPIES]), entry.title, entry.artist,
                             entry.album, entry.fmt, folder, by, dj)
            key = (item.label.lower(), item.format, folder, dj)
            if key in busy:
                skipped += 1
                continue
            busy.add(key)
            self.items[item.id] = item
            queued += 1
        self._changed()
        return queued, skipped

    def get(self, item_id):
        item = self.items.get(item_id)
        if not item:
            raise UserError('That song is no longer in the queue.', 404)
        return item

    def pause(self, item):
        if item.status in ACTIVE:
            self._stop(item, 'paused', 'Paused')

    def resume(self, item):
        if item.status in RESTARTABLE:
            item.status, item.progress, item.speed, item.note, item.finished = 'queued', 0.0, None, '', None
            self._changed()

    def cancel(self, item):
        if item.status in STOPPABLE:
            self._stop(item, 'cancelled', 'Cancelled')

    def delete(self, item):
        self._interrupt(item)
        self.items.pop(item.id, None)
        self._changed()

    def pause_all(self):
        n = 0
        for item in list(self.items.values()):
            if item.status in ACTIVE:
                self._stop(item, 'paused', 'Paused')
                n += 1
        return n

    def retry_failed(self):
        failed = [i for i in self.items.values() if i.status == 'failed']
        for item in failed:
            self.resume(item)
        return len(failed)

    def clear_done(self):
        done = [i for i in self.items.values() if i.status == 'done']
        for item in done:
            del self.items[item.id]
        self._changed()
        return len(done)

    def _stop(self, item, status, note):
        self._interrupt(item)
        item.status, item.speed, item.note = status, None, note
        item.finished = int(time.time() * 1000)
        self._changed()

    def _interrupt(self, item):
        task = self.tasks.get(item.id)
        if task and not task.done():
            task.cancel()

    # Views

    def summary(self):
        """Just the numbers, for the count of active downloads in the menu."""
        return {
            'active': self.active_count(),
            'speed': sum(i.speed or 0 for i in self.items.values() if i.status == 'downloading'),
        }

    def view(self, by=None, recent=None):
        """Every item, or only `by`'s. With `recent`, only that many of the songs
        still waiting and that many of the latest finished ones are listed (plus
        everything downloading or paused), so a big import doesn't send thousands
        of rows; `more` says how many were left out."""
        items = [i for i in self.items.values() if by is None or i.by == by]
        more = 0
        if recent is not None:
            ended = [i for i in items if i.status in ('done', 'failed', 'cancelled')]
            keep = {i.id for i in sorted(ended, key=lambda i: i.finished or i.created)[-recent:]}
            keep |= {i.id for i in [i for i in items if i.status == 'queued'][:recent]}
            shown = [i for i in items if i.status in ('downloading', 'paused') or i.id in keep]
            more = len(items) - len(shown)
            items = shown
        return {'items': [i.view() for i in items], 'more': more, **self.summary()}

    def active_count(self):
        return sum(i.status in ACTIVE for i in self.items.values())

    # Running

    def dest_dir(self, folder):
        """Where a normalize_folder() name is, inside the server folder (the
        downloads folder unless downloads.server_folder is set). The name has no
        "..", drive letters or other way out, so it is only joined, not resolved
        (resolving races with the folder being created)."""
        if folder != normalize_folder(folder):
            raise UserError('That folder is outside the downloads folder.')
        root = self.cfg.server_folder
        return root.joinpath(*folder.split('/')) if folder else root

    def folder_for(self, playlist, dj=False):
        """The folder for a playlist's songs (playlist_folder, or dj_folder for its CDJ copies). When the downloads folder
        sits inside the server folder (music/downloads), a playlist named after it gets
        another name, so its songs never land among the visitors' downloads."""
        folder = base = dj_folder(playlist) if dj else playlist_folder(playlist)
        n = 1
        while self._is_downloads(self.dest_dir(folder)):
            n += 1
            folder = f'{base} ({n})'
        return folder

    def _is_downloads(self, path: Path):
        if path == self.cfg.downloads_dir:
            return True
        try:
            return os.path.samefile(path, self.cfg.downloads_dir)
        except OSError:
            return False

    def _changed(self):
        self.dirty = True
        self.wake.set()

    async def run(self):
        """Starts queued songs as slots free up, and keeps the queue file current."""
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
                log.exception('queue loop failed')

    async def _dispatch(self):
        running = sum(not t.done() for t in self.tasks.values())
        waiting = [i for i in self.items.values() if i.status == 'queued']
        if not waiting or running >= SLOTS:
            return
        # Songs from SoundCloud or YouTube don't need Soulseek, so they still go while it's away
        if any(i.source == 'soulseek' for i in waiting) and not await self.engine.connected():
            waiting = [i for i in waiting if i.source != 'soulseek']
        for item in waiting[:SLOTS - running]:
            item.status, item.progress, item.speed, item.note = 'downloading', 0.0, None, 'Starting'
            task = self.engine.spawn(self._run(item))
            self.tasks[item.id] = task
            task.add_done_callback(lambda _t, i=item.id: self._ended(i))
        self.dirty = True

    def _ended(self, item_id):
        self.tasks.pop(item_id, None)
        self.wake.set()

    async def _run(self, item: QueueItem):
        stem = file_stem(item.title, item.artist)

        def update(progress, speed, note):
            item.progress = progress if progress is not None else 0.0
            item.speed, item.note = speed, note

        try:
            dest = self.dest_dir(item.folder)
            await asyncio.to_thread(dest.mkdir, parents=True, exist_ok=True)
            # A DJ folder only ever gets CDJ copies, so any WAV or MP3 of the song there is one
            exts = DJ_EXTENSIONS if item.dj else matching.EXTENSIONS[item.format]
            have = await asyncio.to_thread(_existing, dest, stem, exts)
            if have:
                self._finish(item, f'Already in the folder as {have}')
                return
            if self.engine.low_disk():
                raise UserError(LOW_DISK)
            fetch = await self.engine.download(item.cands, update)
            if item.dj:
                name, label = await self._save_dj(item, fetch, dest, stem, update)
                note = f'Saved as {name} ({label})'
            else:
                name = await asyncio.to_thread(_save_to, fetch.path, dest, stem)
                self.engine.library.add(dest / name, item.title, item.artist, item.album, fetch.cand)
                if not fetch.observers:
                    await asyncio.to_thread(self.engine.forget, fetch)
                note = f'Saved as {name}'
            item.size = (await asyncio.to_thread((dest / name).stat)).st_size
            self._finish(item, note)
            log.info('saved %s', dest / name)
            self.engine.schedule_rescan()
        except asyncio.CancelledError:
            raise
        except UserError as e:
            self._fail(item, str(e))
        except SlskdError as e:
            log.warning('slskd error on %r: %s', item.label, e)
            self._fail(item, 'Lost contact with Soulseek' if e.status is None else 'Soulseek had a problem')
        except OSError as e:
            log.warning('can\'t save %r: %s', item.label, e)
            self._fail(item, 'The server couldn\'t save the file')
        except Exception:
            log.exception('%r crashed', item.label)
            self._fail(item, 'Something went wrong on the server')

    async def _save_dj(self, item, fetch, dest, stem, update):
        """Saves the CDJ copy of a finished download in the folder. The original stays in its
        download folder, where it's handed out again like any visitor's (and deleted in time)."""
        update(1.0, None, 'Making it CDJ-ready')
        self.engine.library.add(fetch.path, item.title, item.artist, item.album, fetch.cand)
        copy = await self.engine.dj.copy(fetch.path, stem, item.title, item.artist, item.album)
        name = await asyncio.to_thread(_save_to, copy.path, dest, stem)
        if copy.made:
            self.engine.library.add_dj(dest / name, item.title, item.artist, item.album, True, copy.label)
        else:   # the original itself (a CBR MP3), so it's a copy like any other
            self.engine.library.add(dest / name, item.title, item.artist, item.album, fetch.cand)
        return name, copy.label

    def _finish(self, item, note):
        item.status, item.progress, item.speed, item.note = 'done', 1.0, None, note
        item.finished = int(time.time() * 1000)
        self._changed()

    def _fail(self, item, note):
        item.status, item.speed, item.note = 'failed', None, note
        item.finished = int(time.time() * 1000)
        log.info('%s: %s', item.label, note)
        self._changed()

    # Remembering the queue, so it survives a restart

    def load(self):
        try:
            data = json.loads(self.cfg.queue_file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning('can\'t read %s: %s', self.cfg.queue_file, e)
            return
        for d in data.get('items', []):
            try:
                item = QueueItem.from_dict(d)
            except Exception as e:
                log.warning('skipped a saved queue item: %r', e)
                continue
            if item.status == 'downloading':
                item.status, item.note = 'queued', 'Resumed after a restart'
            self.items[item.id] = item
        self.saved_text = None
        log.info('loaded %d queued song(s)', len(self.items))

    def save(self):
        if not self.dirty:
            return
        text = json.dumps({'version': 1, 'items': [i.to_dict() for i in self.items.values()]})
        self.dirty = False
        if text == self.saved_text:
            return
        path = self.cfg.queue_file
        tmp = path.with_name(path.name + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding='utf-8')
            os.replace(tmp, path)
            self.saved_text = text
        except OSError as e:
            self.dirty = True
            log.warning('can\'t save %s: %s', path, e)

    def prune(self):
        cutoff = (time.time() - FINISHED_KEEP_DAYS * 86400) * 1000
        old = [i.id for i in self.items.values()
               if i.status in ('done', 'failed', 'cancelled') and (i.finished or i.created) < cutoff]
        for item_id in old:
            del self.items[item_id]
        if old:
            self._changed()

    def shutdown(self):
        self.dirty = True
        self.save()


def _existing(folder: Path, stem, exts):
    """The name of a file in the folder that is already this song in this format, if any."""
    wanted = {(stem + e).lower() for e in exts}
    try:
        for p in folder.iterdir():
            if p.is_file() and p.name.lower() in wanted:
                return p.name
    except OSError:
        pass
    return None


def _save_to(src: Path, folder: Path, stem):
    """Puts a finished download in the folder as "Artist - Title.ext". A hard
    link when possible so it takes no extra space and survives the download
    being cleaned up later."""
    ext = src.suffix.lower()
    target = folder / (stem + ext)
    n = 1
    while target.exists():
        n += 1
        target = folder / f'{stem} ({n}){ext}'
    try:
        os.link(src, target)
    except OSError:
        shutil.copy2(src, target)
    return target.name
