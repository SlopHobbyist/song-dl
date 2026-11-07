"""Everything that talks to Soulseek through slskd: searching, ranking what
comes back, and fetching files. Songs found on SoundCloud or YouTube instead
(see soundcloud_audio.py, youtube_audio.py) are fetched here too, the same way.

Fetching goes through a Fetch, one per remote file, shared by everything that
wants that file, so two people downloading the same song don't enqueue it
twice. `Engine.download` walks down a ranked list of copies, moving on when an
uploader fails, keeps us queued too long, or stalls.
"""

import asyncio
import heapq
import itertools
import logging
import os
import re
import shutil
import time
import uuid
from collections import deque
from pathlib import Path

from . import dj, matching, soundcloud_audio, youtube_audio
from .library import Library
from .errors import UserError
from .slskd import Slskd, SlskdError

log = logging.getLogger('songdl')

POLL_SECONDS = 1
SEARCH_TIMEOUT_MS = 8000      # slskd ends the search after this long without new responses
SEARCH_MAX_SECONDS = 30       # hard cap on a search, whatever slskd says
RESPONSES_WAIT = 10           # seconds a finished search's responses may take to be listed
RESPONSE_LIMIT = 500
FILE_LIMIT = 10000
QUEUE_WAIT = 90               # seconds to wait in a remote queue before trying another copy
QUEUE_WAIT_LAST = 600         # ...when there is no other copy left
STALL_SECONDS = 45            # seconds without progress before giving up on a transfer
MAX_ATTEMPTS = 5              # copies to try per download
RESCAN_GAP = 120              # minimum seconds between share rescans
MIN_FREE_BYTES = 2 * 10**9    # refuse new downloads when the disk has less room than this
ASKED_WINDOW = 30 * 60        # seconds after asking an uploader for a file that we'll answer their captcha
MOVE_WAIT = 30                # seconds a finished download may take to show up in its folder...
MOVE_SETTLE = 5               # ...and to stop growing once it has
LENGTH_TOLERANCE = 8          # seconds a copy may differ from the length in the CSV and still be preferred
# The Soulseek server bans clients that search too fast (for about 30 minutes). These are
# slsk-batchdl's long-standing safe defaults: at most 34 searches in any 220 seconds.
SEARCHES_PER_WINDOW = 34
SEARCH_WINDOW = 220
# Who goes first when searches have to wait: a visitor's single search, then a playlist
# scan someone is watching, then an admin's background import.
INTERACTIVE, SCAN, IMPORT = 0, 1, 2

NOT_CONNECTED = 'Song-DL is not connected to the Soulseek network right now. Please try again in a minute.'
UNREACHABLE = 'Song-DL lost contact with its Soulseek client. Please try again in a minute.'
LOW_DISK = 'The server is almost out of disk space, so it can\'t download right now.'

# slskd saves each fetch in a folder named by a random UUID under the downloads folder
STAGING = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def mb(n):
    return f'{n / 1e6:.1f} MB'


class Fetch:
    """One remote file being downloaded by slskd."""

    def __init__(self, cand: matching.Candidate):
        self.cand = cand
        self.id = str(uuid.uuid4())  # also the folder it's saved in
        self.transfer_id = None
        self.phase = 'requesting'    # requesting, queued (or waiting, for YouTube), transferring, done, failed
        self.place = None            # place in the uploader's queue
        self.bytes = 0
        self.total = 0               # the size, when the copy's own is only a guess (YouTube)
        self.speed = 0
        self.started = None
        self.finished = None
        self.path = None
        self.error = None
        self.created = time.monotonic()
        self.observers = 0
        self.task = None
        self.cancelled = False       # tells a YouTube download, which runs in a thread, to stop

    @property
    def key(self):
        return self.cand.key

    @property
    def active(self):
        return self.phase not in ('done', 'failed')


class SearchLimiter:
    """Keeps searches under SEARCHES_PER_WINDOW in any SEARCH_WINDOW seconds,
    across the whole site. Waiting searches start in priority order, then in
    the order they asked."""

    def __init__(self, per_window, window):
        self.per_window = per_window
        self.window = window
        self.started = deque()   # monotonic times of the searches in the window
        self.waiting = []        # heap of (priority, ticket)
        self.tickets = itertools.count()

    def _prune(self, now):
        while self.started and now - self.started[0] >= self.window:
            self.started.popleft()

    def wait_estimate(self):
        """Seconds until a search could start now, ignoring anyone already waiting."""
        now = time.monotonic()
        self._prune(now)
        if len(self.started) < self.per_window:
            return 0
        return self.window - (now - self.started[0])

    async def acquire(self, priority=INTERACTIVE, waiting=None):
        """Returns once this search may start. `waiting(seconds)` is called if it has to wait."""
        me = (priority, next(self.tickets))
        heapq.heappush(self.waiting, me)
        told = False
        try:
            while True:
                now = time.monotonic()
                self._prune(now)
                if self.waiting[0] == me and len(self.started) < self.per_window:
                    self.started.append(now)
                    return
                if waiting and not told:
                    told = True
                    waiting(self.wait_estimate())
                await asyncio.sleep(0.25 if self.waiting[0] != me else
                                    min(1.0, max(0.05, self.window - (now - self.started[0]))))
        finally:
            self.waiting.remove(me)
            heapq.heapify(self.waiting)


class Skip:
    """A visitor's request to give up on the copy being fetched and try the next one."""

    def __init__(self):
        self.event = asyncio.Event()
        self.more = False    # whether there is a next copy to try

    def request(self):
        if self.more:
            self.event.set()


class Engine:
    def __init__(self, cfg, slskd: Slskd):
        self.cfg = cfg
        self.slskd = slskd
        self.limiter = SearchLimiter(SEARCHES_PER_WINDOW, SEARCH_WINDOW)
        self.fetches = {}   # (username, filename) -> Fetch
        self.tasks = set()
        self.rescan_pending = False
        self.last_rescan = 0
        self.asked = {}       # username -> monotonic time we last asked them for a file
        self.captcha = None   # captcha.Captcha, when captchas are answered
        self.youtube = youtube_audio.YouTubeAudio(cfg)
        self.soundcloud = soundcloud_audio.SoundCloudAudio(cfg)
        # Where to look when Soulseek doesn't have a song, by source name (matching.SOURCES)
        self.elsewhere = {'soundcloud': self.soundcloud, 'youtube': self.youtube}
        self.library = Library(cfg)   # songs already here, looked at before any of those
        self.dj = dj.Converter(cfg, self.library)   # DJ mode's CDJ-ready copies

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def shutdown(self):
        for task in list(self.tasks):
            task.cancel()

    # Connection

    async def check_connected(self):
        try:
            state = await self.slskd.server_state()
        except SlskdError:
            raise UserError(UNREACHABLE, 503)
        if not state or not state.get('isLoggedIn'):
            raise UserError(NOT_CONNECTED, 503)

    async def connected(self):
        try:
            await self.check_connected()
            return True
        except UserError:
            return False

    def low_disk(self):
        try:
            return shutil.disk_usage(self.cfg.downloads_dir).free < MIN_FREE_BYTES
        except OSError:
            return False

    # Search

    async def search(self, query, fmt, status=None, priority=INTERACTIVE):
        """Searches Soulseek for the query and returns a matching.Ranking of
        the files of one format. `status(text)` is told how the search is going."""
        def waiting(seconds):
            if status:
                status('Waiting for a turn to search. Soulseek limits how often we can search'
                       + (f' (about {round(seconds)} s)' if seconds >= 1 else ''))

        await self.limiter.acquire(priority, waiting)
        search_id = str(uuid.uuid4())
        await self.slskd.start_search(
            search_id, matching.search_text(query, fmt),
            searchTimeout=SEARCH_TIMEOUT_MS, responseLimit=RESPONSE_LIMIT, fileLimit=FILE_LIMIT,
        )
        try:
            responses = await self._collect(search_id, status)
        finally:
            self.spawn(self._quietly(self.slskd.delete_search(search_id)))
        return matching.rank(query, fmt, responses)

    async def _collect(self, search_id, status):
        started = time.monotonic()
        reported = -1
        while True:
            await asyncio.sleep(POLL_SECONDS)
            s = await self.slskd.get_search(search_id)
            if 'Errored' in (s.get('state') or ''):
                raise UserError('The Soulseek search failed. Please try again.', 503)
            if s.get('isComplete'):
                break
            n = s.get('responseCount') or 0
            if status and n != reported:
                status(f'Searching Soulseek: {n} response{"s" * (n != 1)} so far')
                reported = n
            if time.monotonic() - started > SEARCH_MAX_SECONDS:
                await self.slskd.stop_search(search_id)
                await asyncio.sleep(0.5)
                s = await self.slskd.get_search(search_id)
                break
        # slskd can say a search is complete a moment before it has saved the responses, and
        # then lists none (or a few). Read them again until most of those it counted are there.
        expected = s.get('responseCount') or 0
        deadline = time.monotonic() + RESPONSES_WAIT
        while True:
            responses = await self.slskd.search_responses(search_id) or []
            if len(responses) * 2 >= expected or time.monotonic() > deadline:
                return responses
            await asyncio.sleep(0.25)

    async def find_track(self, track, fmt, status=None, priority=SCAN):
        """Searches Soulseek for a song of a playlist and returns the ranked
        copies, or raises UserError('No match found')."""
        cands = (await self.search(track.query, fmt, status, priority)).candidates
        if not cands and track.artist:
            # Soulseek blocks searches for some artists. Search the title alone and
            # keep only files whose path mentions the artist.
            wanted = set(matching.tokens(track.artist))
            alone = (await self.search(track.title_query, fmt, status, priority)).candidates
            cands = [c for c in alone if wanted & set(matching.tokens(c.filename))]
        # A remix is only acceptable if the playlist asks for one
        cands = [c for c in cands if c.relevance >= 1]
        if not cands:
            raise UserError('No match found')
        if track.duration:
            # Stable, so the ranking is kept within each group
            cands.sort(key=lambda c: not (c.length and abs(c.length - track.duration) <= LENGTH_TOLERANCE))
        return cands

    async def find_elsewhere(self, source, track, status=None, more=False):
        """The copies of a playlist's song on SoundCloud or YouTube, best first, or []. `more`:
        somewhere else will be tried after."""
        if status:
            status(f'Searching {matching.SOURCE_LABELS[source]}')
        return await self.elsewhere[source].find(track.query, track.artist, track.title, track.duration,
                                                 more=more)

    # Download

    async def download(self, cands, update=None, skip=None):
        """Fetches the first copy that works and returns its Fetch (see
        fetch.path). `update(progress, speed, note)` is called as it goes;
        progress is 0..1 or None. `skip` (a Skip) lets the visitor give up on the
        copy being fetched for the next one. Raises UserError when no copy works."""
        cands = cands[:MAX_ATTEMPTS]
        last = len(cands) - 1
        for i, cand in enumerate(cands):
            retried = False
            while True:
                fetch = self._fetch(cand)
                fetch.observers += 1
                cancel = None
                try:
                    if skip:
                        skip.more = i < last
                    if await self._watch(fetch, i == last, i, update, skip):
                        return fetch
                finally:
                    fetch.observers -= 1
                    if fetch.active and not fetch.observers:
                        cancel = self.spawn(self._cancel(fetch))
                if skip and skip.event.is_set():
                    skip.event.clear()
                    log.info('visitor skipped the copy from %s', cand.username)
                    break
                # Uploaders with a captcha refuse or hold the file until it's answered; ask once more after that
                if retried or fetch.observers or not await self._captcha_answered(fetch):
                    break
                retried = True
                log.info('asking %s again after answering their captcha', cand.username)
                if update:
                    update(None, None, f'Answered {cand.username}\'s captcha, asking again')
                if cancel:
                    await cancel
                if self.fetches.get(fetch.key) is fetch:
                    del self.fetches[fetch.key]
        if cands and cands[0].source != 'soulseek' and all(c.source == cands[0].source for c in cands):
            where = matching.SOURCE_LABELS[cands[0].source]
            reason = fetch.error if fetch.error and fetch.error != 'Cancelled' else ''
            if reason == f'{where} didn\'t send the audio':   # says nothing the first half doesn't
                reason = ''
            elif reason and not reason.startswith(tuple(matching.SOURCE_LABELS.values())):
                reason = reason[0].lower() + reason[1:]
            text = f'Couldn\'t download it from {where}' + (f': {reason}' if reason else '')
            if 'try again' not in reason:
                text = text.rstrip('.') + '. Please try again later.'
            raise UserError(text)
        raise UserError('Couldn\'t get the file from any uploader. Please try again later, or search again.'
                        if len(cands) > 1 else
                        'Couldn\'t get the file from the uploader. Please try again later, or search again.')

    async def _captcha_answered(self, fetch: Fetch):
        if not self.captcha or fetch.cand.source != 'soulseek':
            return False
        return await self.captcha.answered_since(fetch.cand.username, fetch.created)

    def asked_recently(self, user):
        """Whether we asked `user` for a file lately; only they may get a captcha answered."""
        return time.monotonic() - self.asked.get(user, float('-inf')) < ASKED_WINDOW

    async def _watch(self, fetch: Fetch, is_last, attempt, update, skip=None):
        """Follows a fetch. Returns True once the file is ready, False when this copy is no good."""
        user = fetch.cand.username
        where = matching.SOURCE_LABELS[fetch.cand.source]
        prefix = 'Another copy: ' if attempt else ''
        waiting_since = time.monotonic()
        progress_at, progress_bytes = time.monotonic(), -1

        def report(progress, note):
            if update:
                update(progress, fetch.speed or None if progress is not None else None, prefix + note)

        while True:
            phase = fetch.phase
            if phase == 'done':
                return True
            if skip and skip.more and skip.event.is_set():
                return False
            if phase == 'failed':
                log.info('copy from %s failed: %s', user, fetch.error)
                return False

            if phase == 'transferring':
                size = fetch.total or fetch.cand.size
                report(min(1, fetch.bytes / size) if size else None,
                       'Downloading' if fetch.cand.source == 'soulseek' else f'Downloading from {where}')
                if fetch.bytes != progress_bytes:
                    progress_at, progress_bytes = time.monotonic(), fetch.bytes
                elif time.monotonic() - progress_at > STALL_SECONDS:
                    log.info('transfer from %s stalled', user)
                    return False
            else:
                if phase == 'queued':
                    report(None, f'Queued by the uploader (position {fetch.place})' if fetch.place
                           else 'Queued by the uploader')
                elif phase == 'waiting':
                    report(None, f'Waiting for a turn to download from {where}')
                    waiting_since = time.monotonic()   # not stuck, just busy
                elif fetch.cand.source != 'soulseek':
                    report(None, f'Getting the audio from {where}')
                else:
                    report(None, f'Requesting the file from {user}')
                limit = QUEUE_WAIT_LAST if is_last else QUEUE_WAIT
                if time.monotonic() - waiting_since > limit:
                    log.info('gave up waiting on %s after %d s', user, limit)
                    return False

            await asyncio.sleep(0.5)

    def _fetch(self, cand):
        fetch = self.fetches.get(cand.key)
        if fetch and (fetch.active or (fetch.phase == 'done' and fetch.path and fetch.path.exists())):
            return fetch
        fetch = Fetch(cand)
        self.fetches[fetch.key] = fetch
        fetch.task = self.spawn(self._run_fetch(fetch))
        return fetch

    async def _run_fetch(self, fetch: Fetch):
        cand = fetch.cand
        try:
            if cand.source == 'local':
                await self._local(fetch)
            elif cand.source != 'soulseek':
                await self._elsewhere(fetch)
            else:
                await self._transfer(fetch)
        except SlskdError as e:
            log.info('fetch %s from %s failed: %s', cand.filename, cand.username, e)
            fetch.error = _transfer_error(cand.username, str(e))
            fetch.phase = 'failed'
        except asyncio.CancelledError:
            fetch.phase = 'failed'
            fetch.error = 'Cancelled'
            raise
        except OSError as e:
            if cand.source != 'local':
                raise
            log.info('local copy %s is gone: %s', cand.filename, e)
            fetch.error = 'The copy on the server is gone. Please search again'
            fetch.phase = 'failed'
        except Exception as e:
            if cand.source != 'soulseek':
                log.info('%s download of %s failed: %r', cand.source, cand.url, e)
                fetch.error = (soundcloud_audio if cand.source == 'soundcloud' else youtube_audio).download_error(e)
            else:
                log.exception('fetch crashed')
                fetch.error = f'Something went wrong downloading from {cand.username}'
            fetch.phase = 'failed'

    async def _local(self, fetch: Fetch):
        """Hands out a file that's already on the server: a hard link to it in the fetch's own
        folder (a copy where links don't work), so it's served like any other download."""
        src = Path(fetch.cand.filename)
        folder = self.cfg.downloads_dir / fetch.id
        fetch.started = time.monotonic()
        fetch.path = await asyncio.to_thread(_link, src, folder)
        fetch.bytes = fetch.path.stat().st_size
        fetch.finished = time.monotonic()
        fetch.phase = 'done'
        log.info('handed out %s again', src)

    async def _elsewhere(self, fetch: Fetch):
        """Downloads a SoundCloud track's or YouTube video's audio into the fetch's folder."""
        cand = fetch.cand
        fetch.started = time.monotonic()
        path = await self.elsewhere[cand.source].download(cand, self.cfg.downloads_dir / fetch.id, fetch)
        fetch.finished = time.monotonic()
        fmt = youtube_audio.format_of(path)
        if not fmt or not valid_audio(path, fmt):
            path.unlink(missing_ok=True)
            fetch.error = f'{matching.SOURCE_LABELS[cand.source]} sent something that isn\'t audio'
            fetch.phase = 'failed'
            return
        size = path.stat().st_size
        cand.size = fetch.bytes = size
        cand.fmt = fmt
        fetch.path = path
        fetch.phase = 'done'
        log.info('downloaded %s from %s', path, cand.url)
        self.schedule_rescan()

    async def _transfer(self, fetch: Fetch):
        cand = fetch.cand
        self.asked[cand.username] = time.monotonic()
        res = await self.slskd.enqueue(cand.username, cand.filename, cand.size, fetch.id)
        failures = (res or {}).get('failures') or []
        transfers = ((res or {}).get('batch') or {}).get('transfers') or []
        if failures or not transfers:
            reason = failures[0].get('message') if failures else 'not enqueued'
            raise SlskdError(reason or 'not enqueued', 500)
        fetch.transfer_id = transfers[0]['id']

        position_checked = 0
        while True:
            await asyncio.sleep(POLL_SECONDS)
            t = await self.slskd.get_download(cand.username, fetch.transfer_id)
            state = t.get('state') or ''
            if state.startswith('Completed'):
                fetch.finished = time.monotonic()
                if 'Succeeded' not in state:
                    fetch.error = _end_reason(cand.username, state, t.get('exception'))
                    fetch.phase = 'failed'
                    return
                await self._finish(fetch)
                return
            if state == 'InProgress':
                if fetch.phase != 'transferring':
                    fetch.started = time.monotonic()
                fetch.phase = 'transferring'
                fetch.bytes = t.get('bytesTransferred') or 0
                fetch.speed = round(t.get('averageSpeed') or 0)
            elif 'Queued' in state and 'Remotely' in state:
                fetch.phase = 'queued'
                if time.monotonic() - position_checked > 10:
                    position_checked = time.monotonic()
                    try:
                        place = await self.slskd.queue_position(cand.username, fetch.transfer_id)
                        fetch.place = place if isinstance(place, int) and place > 0 else fetch.place
                    except SlskdError:
                        pass

    async def _finish(self, fetch: Fetch):
        # slskd says a download succeeded before moving it out of its incomplete folder, a copy
        # when that's another mount, so the file can show up late and still be growing
        folder = self.cfg.downloads_dir / fetch.id
        started = changed = time.monotonic()
        last = None
        while True:
            path, size = await asyncio.to_thread(_newest_file, folder)
            now = time.monotonic()
            if (path, size) != last:
                last, changed = (path, size), now
            if size == fetch.cand.size or now - started > MOVE_WAIT or (path and now - changed > MOVE_SETTLE):
                break
            await asyncio.sleep(0.25)
        if not path:
            raise SlskdError('the finished file is missing', 500)
        if size != fetch.cand.size:
            fetch.error = f'The file from {fetch.cand.username} was incomplete'
        elif not valid_audio(path, fetch.cand.fmt):
            fetch.error = f'The file from {fetch.cand.username} isn\'t a real {matching.LABELS[fetch.cand.fmt]}'
        if fetch.error:
            path.unlink(missing_ok=True)
            fetch.phase = 'failed'
            return
        fetch.bytes = size
        fetch.path = path
        fetch.phase = 'done'
        log.info('downloaded %s', path)
        self.schedule_rescan()

    def forget(self, fetch: Fetch):
        """Deletes a finished fetch's staging folder, once its file has been moved elsewhere or is no longer wanted."""
        if self.fetches.get(fetch.key) is fetch:
            del self.fetches[fetch.key]
        folder = self.cfg.downloads_dir / fetch.id
        shutil.rmtree(folder, ignore_errors=True)

    def active_speed(self):
        return sum(f.speed for f in self.fetches.values() if f.phase == 'transferring')

    # Shares

    def schedule_rescan(self):
        """Shares new downloads soon. Many uploaders throttle or refuse users who
        share nothing, so the share should never lag far behind. The downloads
        folder may be inside one of the extra shares, so those count too."""
        if (self.cfg.share or self.cfg.extra_shares) and not self.rescan_pending:
            self.rescan_pending = True
            self.spawn(self._rescan())

    async def _rescan(self):
        await asyncio.sleep(max(0, self.last_rescan + RESCAN_GAP - time.monotonic()))
        self.rescan_pending = False
        self.last_rescan = time.monotonic()
        await self._quietly(self.slskd.rescan_shares())

    async def _cancel(self, fetch: Fetch):
        fetch.cancelled = True
        if fetch.task:
            fetch.task.cancel()
        if fetch.transfer_id:
            await self._quietly(self.slskd.cancel_download(fetch.cand.username, fetch.transfer_id))
        fetch.phase = 'failed'

    async def _quietly(self, coro):
        try:
            await coro
        except SlskdError as e:
            log.debug('ignored slskd error: %s', e)
        except RuntimeError as e:   # the session was closed: shutting down
            log.debug('ignored: %s', e)

    def prune(self):
        for key in [k for k, f in self.fetches.items() if not f.active and not f.observers
                    and (f.phase == 'failed' or not (f.path and f.path.exists()))]:
            del self.fetches[key]
        for user in [u for u in self.asked if not self.asked_recently(u)]:
            del self.asked[user]


def _link(src: Path, folder: Path):
    """Links src into folder and returns the new path. The link sets the file's change time,
    which restarts its clock for deletion (see web.delete_old_downloads); a visitor's file
    gets its modified time reset too, for anything that only looks at that."""
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    try:
        os.link(src, dst)
    except FileExistsError:
        pass
    except OSError:
        shutil.copy2(src, dst)
    if STAGING.match(src.parent.name):
        os.utime(dst)
    return dst


def _newest_file(folder: Path):
    """The newest file in the folder and its size, or (None, -1)."""
    best, best_stat = None, None
    try:
        for p in folder.iterdir():
            st = p.stat()
            if p.is_file() and (best_stat is None or st.st_mtime > best_stat.st_mtime):
                best, best_stat = p, st
    except OSError:
        pass
    return best, (best_stat.st_size if best_stat else -1)


def _transfer_error(user, message):
    m = message.lower()
    if 'connect' in m or 'unreachable' in m or 'timed out' in m:
        return f'Couldn\'t connect to {user}'
    if 'offline' in m:
        return f'{user} is offline'
    if 'reject' in m or 'denied' in m or 'not shared' in m or 'banned' in m:
        return f'{user} refused the download'
    return f'{user} didn\'t accept the download'


def _end_reason(user, state, exception):
    if 'Rejected' in state:
        return f'{user} refused the download' + (f' ({exception})' if exception else '')
    if 'Cancelled' in state:
        return f'{user} cancelled the transfer'
    if 'TimedOut' in state:
        return f'The transfer from {user} timed out'
    return f'The transfer from {user} failed'


def valid_audio(path: Path, fmt):
    """Checks the file starts the way the format says it should."""
    with path.open('rb') as f:
        head = f.read(12)
        if fmt == 'flac':
            if head[:3] == b'ID3':
                head = _after_id3(f)
            return head[:4] == b'fLaC'
        if fmt == 'wav':
            return head[:4] in (b'RIFF', b'RF64') and head[8:12] == b'WAVE'
        if fmt == 'aiff':
            return head[:4] == b'FORM' and head[8:12] in (b'AIFF', b'AIFC')
        if fmt == 'mp3':
            if head[:3] == b'ID3':
                head = _after_id3(f)
            return len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0
        if fmt in ('opus', 'ogg'):
            return head[:4] == b'OggS'
        if fmt == 'm4a':
            return head[4:8] == b'ftyp'
    return False


def _after_id3(f):
    """The bytes following an ID3v2 tag at the start of the file; its size is a 28-bit syncsafe int."""
    f.seek(6)
    b = f.read(4)
    if len(b) < 4:
        return b''
    f.seek(10 + (b[0] << 21 | b[1] << 14 | b[2] << 7 | b[3]))
    return f.read(4)
