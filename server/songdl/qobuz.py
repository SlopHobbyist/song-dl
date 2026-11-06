"""Reads Qobuz playlist, album and song links.

Qobuz's API (www.qobuz.com/api.json/0.2) wants an app id with every request.
The one Qobuz's public share site (open.qobuz.com) uses reads public
playlists, albums and songs without anyone signed in. It is kept here, and read
out of open.qobuz.com's main.js again if Qobuz ever turns it down. (The web
player's own id, at play.qobuz.com, wants a signed-in user.)

Track lists are paged 500 at a time (the most Qobuz gives) until Qobuz's
total is reached.
"""

import asyncio
import logging
import re

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

API = 'https://www.qobuz.com/api.json/0.2/'
OPEN = 'https://open.qobuz.com'
APP_ID = '712109809'   # open.qobuz.com's, seen 2026-11-06
PAGE = 500
MAX_ITEMS = 10_000
TIMEOUT = 30
RETRIES = 4
# www.qobuz.com turns away requests that don't look like a browser's
HEADERS = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'gzip, deflate'}

# open.qobuz.com/playlist/<id>, play.qobuz.com/album/<id>, www.qobuz.com/us-en/album/<name>/<id>,
# www.qobuz.com/us-en/playlists/<name>/<id>
LINK = re.compile(
    r'\b(?:(?:open|play)\.qobuz\.com/(?P<k1>playlist|album|track)/(?P<id1>[A-Za-z0-9]+)'
    r'|(?:www\.)?qobuz\.com/[a-z]{2}-[a-z]{2}/(?P<k2>playlists|album)/[^/?#\s]+/(?P<id2>[A-Za-z0-9]+))\b',
    re.I)
MAIN_SCRIPT = re.compile(r'src="(/resources/[^"]+/js/main\.js)"')
APP_ID_IN_SCRIPT = re.compile(r'qobuzapi=\{app_id:"(\d+)"')

NOT_FOUND = {
    'playlist': 'Qobuz couldn\'t find that playlist. Check the link, and that the playlist is public.',
    'album': 'Qobuz couldn\'t find that album. Check the link.',
    'song': 'Qobuz couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, id) for a Qobuz playlist, album or song link, or None."""
    m = LINK.search(str(text or ''))
    if not m:
        return None
    kind = (m.group('k1') or m.group('k2')).lower().rstrip('s')
    ident = m.group('id1') or m.group('id2')
    if kind in ('playlist', 'track') and not ident.isdigit():
        return None
    return ('song' if kind == 'track' else kind), ident


def _title(t):
    title = t.get('title') or ''
    version = t.get('version')
    return f'{title} ({version})' if version and version.lower() not in title.lower() else title


def _artists(t):
    """The main and featured artists, from the credits ("Name, MainArtist, Composer - Name, …"),
    else the performer. A name can have a comma of its own ("Earth, Wind & Fire, MainArtist"),
    and so can a role ("Bass guitar (all types)"), but MainArtist and FeaturedArtist come
    straight after the name: the name is everything before them."""
    performer = (t.get('performer') or (t.get('album') or {}).get('artist') or {}).get('name') or ''
    names = []
    for part in (t.get('performers') or '').split(' - '):
        bits = [b.strip() for b in part.split(', ')]
        at = next((i for i, b in enumerate(bits) if i and b in ('MainArtist', 'FeaturedArtist')), None)
        if at is None:
            continue
        name = ', '.join(bits[:at])
        if performer and name.startswith(performer + ', '):
            name = performer      # a role before MainArtist
        if name and name not in names:
            names.append(name)
    return names or [performer]


class Qobuz:
    name = 'Qobuz'

    def __init__(self):
        self.app_id = APP_ID

    async def read(self, kind, xid):
        return await (self.song(xid) if kind == 'song' else self.playlist(kind, xid))

    def _session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT), headers=HEADERS)

    async def _scrape_app_id(self, http):
        async with http.get(OPEN + '/') as res:
            html = await res.text()
        m = MAIN_SCRIPT.search(html)
        if not m:
            return None
        async with http.get(OPEN + m.group(1)) as res:
            ids = APP_ID_IN_SCRIPT.findall(await res.text())
        return ids[-1] if ids else None   # the last one is the live site's

    async def _get(self, http, path, kind, **params):
        for attempt in range(RETRIES):
            async with http.get(API + path, params={**params, 'app_id': self.app_id}) as res:
                try:
                    body = await res.json(content_type=None)
                except ValueError:
                    body = None
                status = res.status
            message = str((body or {}).get('message') or '') if isinstance(body, dict) else ''
            if status == 200 and isinstance(body, dict):
                return body
            if status == 404 or (status == 400 and 'argument' in message.lower() and 'app_id' not in message):
                raise UserError(NOT_FOUND[kind], 404)
            if status == 401 and 'not authorized' in message.lower():
                raise UserError(NOT_FOUND[kind], 404)   # a private playlist
            if (status == 400 and 'app_id' in message) or status == 401:
                if attempt == 0 and (app_id := await self._scrape_app_id(http)) and app_id != self.app_id:
                    log.info('qobuz: using open.qobuz.com\'s new app id %s', app_id)
                    self.app_id = app_id
                    continue
                raise ServiceError(f'{path} answered {status}: {message}')
            if status == 429 or status >= 500:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise ServiceError(f'{path} answered {status}: {message or str(body)[:200]}')
        raise ServiceError(f'{path}: gave up after {RETRIES} tries')

    async def playlist(self, kind, xid):
        path, params = (('playlist/get', {'playlist_id': xid, 'extra': 'tracks'}) if kind == 'playlist'
                        else ('album/get', {'album_id': xid}))
        info, items, offset, total = None, [], 0, None
        async with self._session() as http:
            while len(items) < MAX_ITEMS:
                body = await self._get(http, path, kind, limit=PAGE, offset=offset, **params)
                info = info or body
                tracks = body.get('tracks') or {}
                page = tracks.get('items')
                if not isinstance(page, list):
                    raise ServiceError(f'{path} {xid}: no tracks at {offset}')
                total = tracks.get('total', total)
                items += page
                offset += len(page)
                if not page or not isinstance(total, int) or offset >= total:
                    break
        if isinstance(total, int) and len(items) < min(total, MAX_ITEMS):
            raise ServiceError(f'{path} {xid}: got {len(items)} of {total} songs')
        if kind == 'album':
            name = _title(info) or 'Qobuz album'
            owner = ((info.get('artist') or {}).get('name')) or ''
        else:
            name = info.get('name') or 'Qobuz playlist'
            owner = ((info.get('owner') or {}).get('name')) or ''
        album = name if kind == 'album' else ''
        rows = [row(_title(t), _artists(t), (t.get('album') or {}).get('title') or album,
                    (t.get('duration') or 0) * 1000, t.get('isrc'))
                for t in items[:MAX_ITEMS] if isinstance(t, dict) and t.get('title')]
        result = Playlist(xid, name, owner, rows)
        log.info('qobuz %s %s: %r, %d songs', kind, xid, name, len(rows))
        return result

    async def song(self, xid):
        async with self._session() as http:
            t = await self._get(http, 'track/get', 'song', track_id=xid)
        return Song(_title(t), '; '.join(_artists(t)), (t.get('album') or {}).get('title') or '',
                    t.get('duration') or None)
