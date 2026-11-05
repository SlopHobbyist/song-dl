"""HTTP server: the JSON API used by webui/main.js, the web UI itself, and
finished downloads.

Public: the home page, searching, scanning a playlist, and fetching files to
the browser. Behind the admin login: the Server and All pages and the queue API.
"""

import asyncio
import json
import logging
import re
import time
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from . import jobs, matching, playlist, turnstile
from .auth import COOKIE, FAIL_DELAY, Auth
from .engine import STAGING
from .errors import UserError
from .server_queue import ServerQueue

log = logging.getLogger('songdl')

USER = web.RequestKey('user', str)   # who is signed in, or None
MAX_QUERY = 200
MAX_JSON = 16 * 1024
MAX_UPLOAD = 512 * 1024
MAX_ZIP_FILES = 300
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


async def read_json(request):
    try:
        raw = await request.read()
        if len(raw) > MAX_JSON:
            raise UserError('The request was too large.', 413)
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise UserError('The request was not valid JSON.')
    if not isinstance(data, dict):
        raise UserError('The request was not valid JSON.')
    return data


def parse_formats(raw):
    """["wav", "FLAC"] or "wav,flac" -> ['wav', 'flac'], in the order the site lists them."""
    if isinstance(raw, str):
        raw = raw.split(',')
    if not isinstance(raw, list):
        raise UserError('Pick at least one format.')
    chosen = {str(f).strip().lower() for f in raw}
    if not chosen:
        raise UserError('Pick at least one format.')
    unknown = chosen - set(matching.FORMATS)
    if unknown:
        raise UserError('Pick WAV, AIFF, FLAC or MP3.')
    return [f for f in matching.FORMATS if f in chosen]


def content_security_policy(cfg):
    cloudflare = ' https://challenges.cloudflare.com' if cfg.turnstile_site_key else ''
    frames = cloudflare or " 'none'"
    return (f"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            f"script-src 'self'{cloudflare}; connect-src 'self'; font-src 'self'; "
            f"frame-src{frames}; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def build_app(cfg, manager: jobs.Manager, queue: ServerQueue, captcha=None):
    limiter = RateLimiter(cfg.rate_limit)
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
        formats = parse_formats(data.get('formats'))
        if len(query) > MAX_QUERY:
            raise UserError(f'That search is too long. Keep it under {MAX_QUERY} characters.')
        words = [w for w in matching.tokens(query) if w not in matching.FORMATS]
        if not words or sum(map(len, words)) < 3:
            raise UserError('Type a little more of the song name.')
        limit(request, 'search')
        job = await manager.start_search(query, formats)
        log.info('search %s from %s: %r (%s)', job.id, client_ip(request), query, ','.join(formats))
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
        formats = parse_formats(request.query.get('formats', ''))
        limit(request, 'scan')
        tracks, skipped = playlist.parse(await request.read())
        name = ' '.join(request.query.get('name', '').split())[:80] or 'your playlist'
        job = await manager.start_scan(name, formats, tracks, skipped)
        log.info('scan %s from %s: %r, %d songs (%s)', job.id, client_ip(request), name, len(tracks), ','.join(formats))
        return web.json_response({'id': job.id})

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
        for jid in request.query.getall('id', [])[:MAX_ZIP_FILES]:
            job = manager.downloads.get(jid)
            if job:
                job.updated = time.monotonic()
                job.entry.touch()
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

    async def zip_files(request):
        files, used = [], set()
        for jid in request.query.getall('job', [])[:MAX_ZIP_FILES]:
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
        zip_name = clean_name(request.query.get('name'), None, default='songs') + '.zip'
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
        require_admin(request)
        return web.json_response({**queue.view(), 'me': request[USER]})

    async def add_to_queue(request):
        user = require_admin(request)
        data = await read_json(request)
        ids = data.get('results')
        if not isinstance(ids, list) or not ids or len(ids) > MAX_ZIP_FILES * 4:
            raise UserError('Nothing to queue.')
        entries = [manager.entry(i) for i in ids]
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

    app = web.Application(middlewares=[middleware], client_max_size=MAX_UPLOAD)
    app.add_routes([
        web.get('/api/session', session),
        web.post('/api/login', login),
        web.post('/api/logout', logout),
        web.post('/api/search', start_search),
        web.get('/api/search/{id}', poll_search),
        web.post('/api/scan', start_scan),
        web.get('/api/scan/{id}', poll_scan),
        web.delete('/api/scan/{id}', cancel_scan),
        web.post('/api/download', start_download),
        web.get('/api/downloads', poll_downloads),
        web.get('/api/zip', zip_files),
        web.get('/api/queue', get_queue),
        web.post('/api/queue', add_to_queue),
        web.post('/api/queue/pause-all', pause_all),
        web.post('/api/queue/clear-done', clear_done),
        web.post('/api/queue/{id}/pause', queue_action(queue.pause)),
        web.post('/api/queue/{id}/resume', queue_action(queue.resume)),
        web.post('/api/queue/{id}/cancel', queue_action(queue.cancel)),
        web.delete('/api/queue/{id}', delete_queued),
        web.get('/api/health', health),
        web.get('/files/{folder}/{name}', serve_file),
        web.get('/', page),
        web.get('/{path:.+}', page),
    ])

    async def background(app):
        if not auth.enabled:
            log.warning('webserver.admins is empty: the Server and All pages are turned off')
        queue.load()
        await manager.engine.slskd.start()
        tasks = [
            asyncio.create_task(housekeeping(cfg, manager, queue, limiter, auth)),
            asyncio.create_task(queue.run()),
        ]
        if captcha:
            await captcha.start()
            tasks.append(asyncio.create_task(captcha.run()))
        else:
            log.info('captcha.enabled is off, so uploaders\' captcha messages won\'t be answered')
        yield
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
