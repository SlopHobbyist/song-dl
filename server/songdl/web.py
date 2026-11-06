"""HTTP server: the JSON API used by webui/main.js, the web UI itself, and
finished downloads.

Public: the home page, searching, reading a playlist link into a CSV, scanning
a playlist, and fetching files to the browser. Behind the admin login: the
Server and All pages, the queue API and playlist imports.
"""

import asyncio
import json
import logging
import re
import secrets
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import imports, jobs, matching, playlist, spotify, turnstile, youtube
from .auth import COOKIE, FAIL_DELAY, Auth
from .engine import STAGING
from .errors import UserError
from .server_queue import ServerQueue

log = logging.getLogger('songdl')

USER = web.RequestKey('user', str)   # who is signed in, or None
MAX_QUERY = 200
MAX_JSON = 64 * 1024
MAX_UPLOAD = 512 * 1024              # a CSV for a browser scan (up to playlist.MAX_TRACKS songs)
MAX_IMPORT = 8 * 1024 * 1024         # a CSV for an admin's import (up to imports.MAX_TRACKS songs)
MAX_FILES = playlist.MAX_TRACKS * len(matching.FORMATS)   # a whole scan in every format
ZIP_TTL = 3600
IMPORTER = web.AppKey('importer', imports.Importer)
ZIP_CHUNK = 1024 * 1024
CLEANUP_SECONDS = 3600

# The pages by the address the browser shows, and the file in webui/ behind each
PAGES = {'/': 'index.html', '/login/': 'login/index.html',
         '/server/': 'server/index.html', '/all/': 'all/index.html'}
# Pages only admins may open
ADMIN_PAGES = {'/server/', '/all/'}
# Other spellings of a page's address (old .html links, no trailing slash) and where they go
PAGE_ALIASES = {'/index.html': '/'} | {
    alias: page for page in PAGES if page != '/'
    for alias in (page.rstrip('/'), page.rstrip('/') + '.html', page + 'index.html')}
# The other files the web server hands out from webui/: the site's files and the
# kit's stylesheet, script and fonts (the kit's folder also holds docs and a .git)
PUBLIC_FILE = re.compile(
    r'^/(style\.css|main\.js'
    r'|vendored/web-design-wavefront/(design_language\.(css|js)|assets/fonts/[\w.-]+\.woff2))$')


class RateLimiter:
    """Sliding one-minute window per client IP and kind of request."""

    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.hits = defaultdict(deque)

    def allow(self, key):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True

    def prune(self):
        now = time.monotonic()
        for key in [k for k, q in self.hits.items() if not q or now - q[-1] > 60]:
            del self.hits[key]


def error(message, status):
    return web.json_response({'error': message}, status=status)


async def read_json(request, max_size=MAX_JSON):
    try:
        if (request.content_length or 0) > max_size:
            raise UserError('The request was too large.', 413)
        raw = await request.read()
        if len(raw) > max_size:
            raise UserError('The request was too large.', 413)
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UserError('The request was not valid JSON.')
    if not isinstance(data, dict):
        raise UserError('The request was not valid JSON.')
    return data


def parse_formats(raw):
    """["FLAC", "wav"] or "flac,wav" -> ['flac', 'wav']: the formats wanted, in the order given,
    which is the order they're tried in. Only the first one found is fetched."""
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        raise UserError('Pick at least one format.')
    chosen = [f for f in dict.fromkeys(str(f).strip().lower() for f in raw) if f]
    if not chosen:
        raise UserError('Pick at least one format.')
    if set(chosen) - set(matching.FORMATS):
        raise UserError('Pick WAV, AIFF, FLAC or MP3.')
    return chosen


def parse_sources(raw):
    """["youtube", "soulseek"] or "soulseek,youtube" -> where to look, in the order given, which is
    the order they're tried in. Left out, only Soulseek."""
    if raw is None or raw == '':
        return ['soulseek']
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        raise UserError('Pick at least one place to look.')
    chosen = list(dict.fromkeys(str(s).strip().lower() for s in raw))
    chosen = [s for s in chosen if s]
    if not chosen:
        raise UserError('Pick at least one place to look.')
    if set(chosen) - set(matching.SOURCES):
        raise UserError('Pick Soulseek, SoundCloud or YouTube.')
    return chosen


def parse_search_formats(raw, sources):
    """The formats to search Soulseek in; none are needed when Soulseek isn't searched."""
    if 'soulseek' not in sources and not raw:
        return []
    try:
        return parse_formats(raw)
    except UserError:
        if 'soulseek' in sources:
            raise
        return []


def content_security_policy(cfg):
    cloudflare = ' https://challenges.cloudflare.com' if cfg.turnstile_site_key else ''
    frames = cloudflare or " 'none'"
    return (f"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self'{cloudflare}; connect-src 'self'; font-src 'self'; "
            f"frame-src{frames}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def build_app(cfg, manager: jobs.Manager, queue: ServerQueue, captcha=None):
    limiter = RateLimiter(cfg.rate_limit)
    importer = imports.Importer(cfg, manager.engine, queue)
    zips = {}   # token -> (time made, [download job ids], name)
    spotify_reader = spotify.Spotify()
    youtube_reader = youtube.YouTube()
    auth = Auth(cfg.admins)
    csp = content_security_policy(cfg)
    webui = cfg.webui_dir

    def client_ip(request):
        if cfg.trust_proxy:
            ip = request.headers.get('CF-Connecting-IP')
            if not ip:
                ip = request.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
            if ip:
                return ip
        return request.remote or ''

    def is_https(request):
        if cfg.https or request.secure:
            return True
        return cfg.trust_proxy and request.headers.get('X-Forwarded-Proto', '').lower() == 'https'

    def limit(request, kind):
        """Rate limits visitors; admins are exempt."""
        if cfg.rate_limiting and not request[USER] and not limiter.allow((client_ip(request), kind)):
            raise UserError('Too many requests. Please wait a minute and try again.', 429)

    def require_admin(request):
        if not request[USER]:
            raise UserError('Please sign in.', 401)
        return request[USER]

    @web.middleware
    async def middleware(request, handler):
        request[USER] = auth.user(request.cookies.get(COOKIE))
        try:
            if request.path in ADMIN_PAGES and not request[USER]:
                raise web.HTTPFound('/login/')
            response = await handler(request)
        except UserError as e:
            response = error(str(e), e.status)
        except web.HTTPException as e:
            if not request.path.startswith('/api/'):
                raise
            response = error(e.reason, e.status)
        response.headers.setdefault('Content-Security-Policy', csp)
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('Referrer-Policy', 'no-referrer')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        if request.path.startswith('/api/') or request.path in PAGES:
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    # Session

    async def session(request):
        return web.json_response({
            'user': request[USER],
            'adminEnabled': auth.enabled,
            'turnstileSiteKey': cfg.turnstile_site_key or None,
            'serverFolder': cfg.server_folder.name or 'downloads',
        })

    async def login(request):
        if not auth.enabled:
            raise UserError('Admin sign-in is not set up on this server.', 403)
        data = await read_json(request)
        ip = client_ip(request)
        wait = auth.retry_after(ip)
        if wait:
            minutes = -(-wait // 60)
            raise UserError(f'Too many wrong passwords. Try again in {minutes} minute{"s" * (minutes != 1)}.', 429)
        if cfg.turnstile_secret and not await turnstile.verify(cfg.turnstile_secret, data.get('turnstile'), ip):
            raise UserError('Please complete the security check and try again.', 400)
        username, password = data.get('username'), data.get('password')
        name = None
        if isinstance(username, str) and isinstance(password, str) and len(username) <= 256 and len(password) <= 256:
            name = auth.check(ip, username.strip(), password)
        if not name:
            log.warning('failed login from %s', ip)
            await asyncio.sleep(FAIL_DELAY)
            raise UserError('That username or password is not right.', 401)
        token, max_age = auth.create_session(name, bool(data.get('remember')))
        resp = web.json_response({'ok': True, 'user': name})
        resp.set_cookie(COOKIE, token, max_age=max_age, path='/', httponly=True,
                        samesite='Strict', secure=is_https(request))
        log.info('%s signed in from %s', name, ip)
        return resp

    async def logout(request):
        auth.end_session(request.cookies.get(COOKIE))
        resp = web.json_response({'ok': True})
        resp.del_cookie(COOKIE, path='/')
        return resp

    # Search and playlist scan

    async def start_search(request):
        data = await read_json(request)
        query = ' '.join(str(data.get('query') or '').split())
        sources = parse_sources(data.get('sources'))
        formats = parse_search_formats(data.get('formats'), sources)
        video = youtube.video_id(f'https://youtu.be/{data["video"]}') if isinstance(data.get('video'), str) else None
        if len(query) > MAX_QUERY:
            raise UserError(f'That search is too long. Keep it under {MAX_QUERY} characters.')
        words = [w for w in matching.tokens(query) if w not in matching.FORMATS]
        if not words or sum(map(len, words)) < 3:
            raise UserError('Type a little more of the song name.')
        limit(request, 'search')
        job = await manager.start_search(query, formats, sources, video)
        log.info('search %s from %s: %r (%s; %s)', job.id, client_ip(request), query, ','.join(formats),
                 ','.join(sources))
        return web.json_response({'id': job.id})

    def find(table, request, what):
        job = table.get(request.match_info['id'])
        if not job:
            raise UserError(f'That {what} has expired. Please start again.', 404)
        return job

    async def poll_search(request):
        return web.json_response(find(manager.searches, request, 'search').view())

    async def start_scan(request):
        if (request.content_length or 0) > MAX_UPLOAD:
            raise UserError('That file is too big. A playlist CSV should be well under 500 KB.', 413)
        sources = parse_sources(request.query.get('sources'))
        formats = parse_search_formats(request.query.get('formats', ''), sources)
        limit(request, 'scan')
        raw = await request.read()
        if len(raw) > MAX_UPLOAD:
            raise UserError('That file is too big. A playlist CSV should be well under 500 KB.', 413)
        tracks, skipped = playlist.parse(raw)
        name = ' '.join(request.query.get('name', '').split())[:80] or 'your playlist'
        job = await manager.start_scan(name, formats, tracks, skipped, sources)
        log.info('scan %s from %s: %r, %d songs (%s; %s)', job.id, client_ip(request), name, len(tracks),
                 ','.join(formats), ','.join(sources))
        return web.json_response({'id': job.id})

    async def playlist_link(request):
        """A Spotify or YouTube playlist link -> the whole playlist as a CSV. The page offers
        the CSV to save, and scans or imports it like any uploaded file."""
        data = await read_json(request)
        link = str(data.get('url') or '').strip()[:2000]
        if pid := spotify.playlist_id(link):
            source, reader = 'spotify', spotify_reader
        elif pid := youtube.playlist_id(link):
            source, reader = 'youtube', youtube_reader
        else:
            raise UserError('That isn\'t a Spotify or YouTube playlist link. It should look like '
                            'https://open.spotify.com/playlist/… or https://www.youtube.com/playlist?list=…')
        limit(request, 'link')
        found = await reader.playlist(pid)
        unavailable = getattr(found, 'unavailable', 0)
        log.info('playlist link from %s: %s %s, %r, %d songs', client_ip(request), source, pid, found.name, len(found.rows))
        return web.json_response({
            'source': source,
            'name': found.name,
            'owner': found.owner,
            'total': len(found.rows) - unavailable,
            'unavailable': unavailable,
            'csv': found.csv(),
            'filename': clean_name(found.name, None, default='playlist') + '.csv',
            'maxScan': playlist.MAX_TRACKS,
            'maxImport': imports.MAX_TRACKS,
        })

    async def video_link(request):
        """A link to a single YouTube video -> the song search for it."""
        data = await read_json(request)
        vid = youtube.video_id(str(data.get('url') or '').strip()[:2000])
        if not vid:
            raise UserError('That isn\'t a link to a YouTube video.')
        limit(request, 'link')
        found = await youtube.video(vid)
        query = playlist.Track(found.song, found.artist).query[:MAX_QUERY].strip()
        return web.json_response({'query': query or found.title[:MAX_QUERY], 'video': vid, 'artist': found.artist,
                                  'song': found.song, 'title': found.title, 'channel': found.channel})

    async def poll_scan(request):
        return web.json_response(find(manager.scans, request, 'playlist scan').view())

    async def cancel_scan(request):
        job = manager.scans.get(request.match_info['id'])
        if job:
            job.cancel()
        return web.json_response({'ok': True})

    # Fetching for the browser

    async def start_download(request):
        data = await read_json(request)
        job = await manager.start_download(data.get('result'), client_ip(request))
        return web.json_response({'id': job.id})

    async def poll_downloads(request):
        views = {}
        for jid in request.query.getall('id', [])[:MAX_FILES]:
            job = manager.downloads.get(jid)
            if job:
                job.seen()
            views[jid] = job.view() if job else {'state': 'failed', 'error': 'That download has expired. Please try again.'}
        return web.json_response({'jobs': views})

    async def serve_file(request):
        folder, name = request.match_info['folder'], request.match_info['name']
        root = cfg.downloads_dir.resolve()
        path = (root / folder / name).resolve()
        if not STAGING.match(folder) or path.parent.parent != root or not path.is_file():
            raise web.HTTPNotFound(text='This file is no longer available. Please search again.')
        download_name = clean_name(request.query.get('as'), path)
        return web.FileResponse(path, headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(download_name)}",
            'Cache-Control': 'private, max-age=3600',
        })

    async def make_zip(request):
        """Remembers which finished downloads go in a zip, and gives back the address that
        streams it. (A few hundred job ids are too long to fit in that address.)"""
        data = await read_json(request)
        ids = data.get('jobs')
        if not isinstance(ids, list) or not ids:
            raise UserError('There\'s nothing to zip.')
        now = time.monotonic()
        for token in [t for t, z in zips.items() if now - z[0] > ZIP_TTL]:
            del zips[token]
        token = secrets.token_urlsafe(12)
        zips[token] = (now, [str(i) for i in ids[:MAX_FILES]], data.get('name'))
        return web.json_response({'url': f'/api/zip/{token}'})

    async def zip_files(request):
        made = zips.get(request.match_info['token'])
        if not made:
            raise UserError('That zip has expired. Please download again.', 404)
        _, ids, zip_name = made
        files, used = [], set()
        for jid in ids:
            job = manager.downloads.get(jid)
            if not job or job.state != 'done' or not job.path or not job.path.exists():
                continue
            name = job.name
            stem, ext = name.rsplit('.', 1) if '.' in name else (name, '')
            n = 1
            while name.lower() in used:
                n += 1
                name = f'{stem} ({n}).{ext}' if ext else f'{stem} ({n})'
            used.add(name.lower())
            files.append((name, job.path))
        if not files:
            raise UserError('There\'s nothing to zip. The files may have expired.', 404)
        zip_name = clean_name(zip_name, None, default='songs') + '.zip'
        resp = web.StreamResponse(headers={
            'Content-Type': 'application/zip',
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(zip_name)}",
            'Cache-Control': 'no-store',
        })
        await resp.prepare(request)
        sink = _Sink()
        with zipfile.ZipFile(sink, 'w', zipfile.ZIP_STORED) as zf:   # audio doesn't compress
            for name, path in files:
                info = zipfile.ZipInfo.from_file(path, name)
                info.compress_type = zipfile.ZIP_STORED
                with path.open('rb') as src, zf.open(info, 'w', force_zip64=True) as dst:
                    while chunk := src.read(ZIP_CHUNK):
                        dst.write(chunk)
                        await sink.drain(resp)
                await sink.drain(resp)
        await sink.drain(resp)
        await resp.write_eof()
        return resp

    # The server queue (admins only)

    async def get_queue(request):
        """The whole queue; ?summary=1 for just the counts; ?mine=1 for the signed-in admin's
        songs with only the latest finished ones."""
        user = require_admin(request)
        if request.query.get('summary'):
            return web.json_response(queue.summary())
        if request.query.get('mine'):
            return web.json_response({**queue.view(by=user, recent=100), 'me': user})
        return web.json_response({**queue.view(), 'me': user})

    async def add_to_queue(request):
        user = require_admin(request)
        data = await read_json(request)
        ids = data.get('results')
        if not isinstance(ids, list) or not ids or len(ids) > MAX_FILES:
            raise UserError('Nothing to queue.')
        entries = [manager.entry(i) for i in ids]
        if any(e.source == 'soulseek' for e in entries):
            await manager.engine.check_connected()
        queued, skipped = queue.add(entries, data.get('folder'), user)
        log.info('%s queued %d song(s) in %r', user, queued, data.get('folder'))
        return web.json_response({'queued': queued, 'skipped': skipped})

    def queue_action(action):
        async def handler(request):
            require_admin(request)
            action(queue.get(request.match_info['id']))
            return web.json_response({'ok': True})
        return handler

    async def delete_queued(request):
        require_admin(request)
        queue.delete(queue.get(request.match_info['id']))
        return web.json_response({'ok': True})

    async def pause_all(request):
        require_admin(request)
        return web.json_response({'paused': queue.pause_all()})

    async def clear_done(request):
        require_admin(request)
        return web.json_response({'cleared': queue.clear_done()})

    # Playlist imports (admins only)

    async def add_import(request):
        """{csv, name, formats, folder}: the whole playlist, searched in the background."""
        user = require_admin(request)
        data = await read_json(request, MAX_IMPORT)
        text = data.get('csv')
        if not isinstance(text, str) or not text.strip():
            raise UserError('Choose a playlist CSV or paste a playlist link first.')
        sources = parse_sources(data.get('sources'))
        formats = parse_search_formats(data.get('formats'), sources)
        tracks, skipped = playlist.parse(
            text.encode('utf-8'), imports.MAX_TRACKS,
            'That playlist has {n} songs. An import can have up to {limit}; split it into smaller files.')
        name = ' '.join(str(data.get('name') or '').split())[:120]
        name = re.sub(r'\.csv$', '', name, flags=re.I) or 'Playlist'
        imp = importer.add(name, formats, data.get('folder'), user, tracks, skipped, sources)
        return web.json_response(imp.view())

    async def list_imports(request):
        require_admin(request)
        return web.json_response(importer.view())

    def import_action(action):
        async def handler(request):
            require_admin(request)
            action(importer.get(request.match_info['id']))
            return web.json_response({'ok': True})
        return handler

    async def missing_csv(request):
        require_admin(request)
        imp = importer.get(request.match_info['id'])
        name = clean_name(imp.name + ' - not found', None, default='not found') + '.csv'
        return web.Response(text=imp.missing_csv(), content_type='text/csv', headers={
            'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"})

    async def health(request):
        try:
            state = await manager.engine.slskd.server_state()
            connected = bool(state and state.get('isLoggedIn'))
        except jobs.SlskdError:
            connected = False
        return web.json_response({'ok': True, 'soulseek': connected})

    async def page(request):
        path = request.path
        if path in PAGE_ALIASES:
            query = request.query_string
            raise web.HTTPMovedPermanently(PAGE_ALIASES[path] + ('?' + query if query else ''))
        if path in PAGES:
            file = PAGES[path]
        elif PUBLIC_FILE.match(path):
            file = path.lstrip('/')
        else:
            raise web.HTTPNotFound()
        return web.FileResponse(webui / file, headers={'Cache-Control': 'no-cache'})

    app = web.Application(middlewares=[middleware], client_max_size=MAX_IMPORT)
    app[IMPORTER] = importer
    app.add_routes([
        web.get('/api/session', session),
        web.post('/api/login', login),
        web.post('/api/logout', logout),
        web.post('/api/search', start_search),
        web.get('/api/search/{id}', poll_search),
        web.post('/api/playlist-link', playlist_link),
        web.post('/api/video-link', video_link),
        web.post('/api/scan', start_scan),
        web.get('/api/scan/{id}', poll_scan),
        web.delete('/api/scan/{id}', cancel_scan),
        web.post('/api/download', start_download),
        web.get('/api/downloads', poll_downloads),
        web.post('/api/zip', make_zip),
        web.get('/api/zip/{token}', zip_files),
        web.get('/api/queue', get_queue),
        web.post('/api/queue', add_to_queue),
        web.post('/api/queue/pause-all', pause_all),
        web.post('/api/queue/clear-done', clear_done),
        web.post('/api/queue/{id}/pause', queue_action(queue.pause)),
        web.post('/api/queue/{id}/resume', queue_action(queue.resume)),
        web.post('/api/queue/{id}/cancel', queue_action(queue.cancel)),
        web.delete('/api/queue/{id}', delete_queued),
        web.get('/api/imports', list_imports),
        web.post('/api/imports', add_import),
        web.get('/api/imports/{id}/missing.csv', missing_csv),
        web.post('/api/imports/{id}/pause', import_action(importer.pause)),
        web.post('/api/imports/{id}/resume', import_action(importer.resume)),
        web.post('/api/imports/{id}/cancel', import_action(importer.cancel)),
        web.delete('/api/imports/{id}', import_action(importer.delete)),
        web.get('/api/health', health),
        web.get('/files/{folder}/{name}', serve_file),
        web.get('/', page),
        web.get('/{path:.+}', page),
    ])

    async def background(app):
        if not auth.enabled:
            log.warning('webserver.admins is empty: the Server and All pages are turned off')
        queue.load()
        importer.load()
        await manager.engine.slskd.start()
        tasks = [
            asyncio.create_task(housekeeping(cfg, manager, queue, limiter, auth)),
            asyncio.create_task(queue.run()),
            asyncio.create_task(importer.run()),
        ]
        if captcha:
            await captcha.start()
            tasks.append(asyncio.create_task(captcha.run()))
        else:
            log.info('captcha.enabled is off, so uploaders\' captcha messages won\'t be answered')
        yield
        importer.shutdown()
        queue.shutdown()
        for task in tasks:
            task.cancel()
        manager.shutdown()
        if captcha:
            await captcha.close()
        await manager.engine.slskd.close()

    app.cleanup_ctx.append(background)
    return app


def clean_name(raw, path, default=None):
    """A file name to offer the browser: what was asked for if it is safe, else the file's own."""
    name = jobs.ILLEGAL.sub('_', ' '.join(str(raw or '').split())).strip(' .')[:200]
    if not name:
        return path.name if path else (default or 'download')
    if path and not name.lower().endswith(path.suffix.lower()):
        name += path.suffix
    return name


class _Sink:
    """What a streamed zip is written to. It has no seek(), so zipfile writes
    sizes after each file instead of going back, and nothing is kept on disk."""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def write(self, data):
        self.buf += data
        self.pos += len(data)
        return len(data)

    def tell(self):
        return self.pos

    def flush(self):
        pass

    async def drain(self, resp):
        if self.buf:
            data, self.buf = bytes(self.buf), bytearray()
            await resp.write(data)


async def housekeeping(cfg, manager, queue, limiter, auth):
    """Forgets old jobs every minute; deletes old downloads every hour."""
    last_cleanup = 0
    while True:
        await asyncio.sleep(60)
        try:
            manager.prune()
            queue.prune()
            limiter.prune()
            auth.prune()
            if time.monotonic() - last_cleanup >= CLEANUP_SECONDS:
                last_cleanup = time.monotonic()
                removed = await asyncio.to_thread(delete_old_downloads, cfg.downloads_dir, cfg.max_file_age)
                if removed:
                    log.info('deleted %d old downloads', removed)
                    manager.engine.schedule_rescan()
        except Exception:
            log.exception('housekeeping failed')


def delete_old_downloads(root: Path, max_age_days):
    """Deletes files fetched for the browser once they are old. Only the
    UUID-named folders slskd saves them in are touched, never the folders
    admins save songs into."""
    if max_age_days <= 0 or not root.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir() or not STAGING.match(folder.name):
            continue
        for f in folder.rglob('*'):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        for d in sorted((d for d in folder.rglob('*') if d.is_dir()), reverse=True):
            _rmdir(d)
        _rmdir(folder)
    return removed


def _rmdir(d):
    try:
        d.rmdir()
    except OSError:
        pass
