"""Reads Tidal playlist, album and song links.

tidal.com and listen.tidal.com put a bot check in front of the web player, but
Tidal's API (api.tidal.com/v1) and its embeddable player (embed.tidal.com)
answer anyone. The embed player signs in with an OAuth client of its own,
whose id and secret are in its script; a client_credentials grant with them
gives a token good for four hours. The pair is kept here, and read out of the
embed player's script again if Tidal ever turns it down. The old web player's
X-Tidal-Token is the last resort.

Track lists are paged 100 at a time (the most Tidal gives) until the count
Tidal gives is reached. Everything is read in the US catalog.
"""

import asyncio
import logging
import re
import time

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

API = 'https://api.tidal.com/v1/'
AUTH = 'https://auth.tidal.com/v1/oauth2/token'
EMBED = 'https://embed.tidal.com'
EMBED_PAGE = EMBED + '/tracks/570268437'   # any embed page has the script
CLIENT = ('TPIsV0A9lyiqKl9u', '2f0kZvP3WX1lv5SMBuaCRpBvHxidgJHc5tikjDw113w=')   # seen 2026-11-06
LEGACY_TOKEN = 'CzET4vdadNUFQ5JU'
COUNTRY = 'US'
PAGE = 100
MAX_ITEMS = 10_000
TIMEOUT = 30
RETRIES = 4

# tidal.com/browse/playlist/<uuid>, listen.tidal.com/album/<id>, tidal.com/track/<id>,
# /album/<id>/track/<id>; plural forms too, as the embed player writes them
LINK = re.compile(
    r'\b(?:www\.|listen\.|embed\.)?tidal\.com/(?:browse/)?'
    r'(?:album/\d+/track/(?P<track>\d+)|(?P<kind>playlists?|albums?|tracks?)/(?P<id>[0-9a-f]{8}-[0-9a-f-]{27}|\d+))\b',
    re.I)
APP_SCRIPT = re.compile(r'src="(/embed-resources/js/app-[^"]+\.js)"')
CHUNK = re.compile(r'from"\.\./(chunk-[A-Za-z0-9]+\.js)"')
CLIENT_IN_SCRIPT = re.compile(r'clientId:"([A-Za-z0-9]+)",clientSecret:"([^"]+)"')

NOT_FOUND = {
    'playlist': 'Tidal couldn\'t find that playlist. Check the link, and that the playlist is public.',
    'album': 'Tidal couldn\'t find that album. Check the link.',
    'song': 'Tidal couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, id) for a Tidal playlist, album or song link, or None."""
    m = LINK.search(str(text or ''))
    if not m:
        return None
    if m.group('track'):
        return 'song', m.group('track')
    kind, ident = m.group('kind').lower().rstrip('s'), m.group('id').lower()
    if (kind == 'playlist') != ('-' in ident):
        return None   # playlists have uuids, albums and songs numbers
    return ('song' if kind == 'track' else kind), ident


def _title(t):
    title = t.get('title') or ''
    version = t.get('version')
    return f'{title} ({version})' if version and version.lower() not in title.lower() else title


def _artists(t):
    names = [a.get('name') for a in t.get('artists') or []]
    return [n for n in names if n] or [((t.get('artist') or {}).get('name')) or '']


class Tidal:
    name = 'Tidal'

    def __init__(self):
        self.client = CLIENT
        self.token = None
        self.legacy = False
        self.token_expires = 0.0
        self.lock = asyncio.Lock()

    async def read(self, kind, xid):
        return await (self.song(xid) if kind == 'song' else self.playlist(kind, xid))

    def _session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                     headers={'User-Agent': USER_AGENT})

    async def _grant(self, http, client):
        async with http.post(AUTH, data={'grant_type': 'client_credentials', 'client_id': client[0],
                                         'client_secret': client[1]}) as res:
            body = await res.json(content_type=None) if res.status == 200 else None
        if not isinstance(body, dict) or not body.get('access_token'):
            return None
        return body['access_token'], time.time() + int(body.get('expires_in') or 3600)

    async def _scrape_client(self, http):
        """The embed player's client id and secret, from its script and the chunks it loads."""
        async with http.get(EMBED_PAGE) as res:
            html = await res.text()
        m = APP_SCRIPT.search(html)
        if not m:
            return None
        base = EMBED + m.group(1).rsplit('/', 2)[0] + '/'
        async with http.get(EMBED + m.group(1)) as res:
            scripts = [await res.text()]
        for chunk in sorted(set(CHUNK.findall(scripts[0]))):
            async with http.get(base + chunk) as res:
                scripts.append(await res.text())
        for script in scripts:
            if m := CLIENT_IN_SCRIPT.search(script):
                return m.group(1), m.group(2)
        return None

    async def _headers(self, http):
        async with self.lock:
            if not self.token or time.time() > self.token_expires - 60:
                got = await self._grant(http, self.client)
                if not got:
                    client = await self._scrape_client(http)
                    if client and client != self.client:
                        log.info('tidal: using the embed player\'s new client %s', client[0])
                        self.client = client
                        got = await self._grant(http, client)
                if got:
                    (self.token, self.token_expires), self.legacy = got, False
                else:
                    log.warning('tidal: no token from the embed player\'s client; using the old web token')
                    self.token, self.token_expires, self.legacy = LEGACY_TOKEN, time.time() + 3600, True
            return {'X-Tidal-Token': self.token} if self.legacy else {'Authorization': f'Bearer {self.token}'}

    async def _get(self, http, path, kind, **params):
        params['countryCode'] = COUNTRY
        for attempt in range(RETRIES):
            headers = await self._headers(http)
            async with http.get(API + path, params=params, headers=headers) as res:
                try:
                    body = await res.json(content_type=None)
                except ValueError:
                    body = None
                status = res.status
            if status == 200 and isinstance(body, dict):
                return body
            if status == 404:
                log.info('tidal %s: %s', path, (body or {}).get('userMessage'))
                raise UserError(NOT_FOUND[kind], 404)
            if status in (401, 403) and attempt == 0:
                self.token = None          # the token ran out or was turned down: get a new one
                continue
            if status == 429 or status >= 500:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise ServiceError(f'{path} answered {status}: {str(body)[:200]}')
        raise ServiceError(f'{path}: gave up after {RETRIES} tries')

    async def playlist(self, kind, xid):
        path = f'{kind}s/{xid}'
        async with self._session() as http:
            info = await self._get(http, path, kind)
            items, offset, total = [], 0, None
            while len(items) < MAX_ITEMS:
                body = await self._get(http, f'{path}/{"items" if kind == "playlist" else "tracks"}', kind,
                                       limit=PAGE, offset=offset)
                page = body.get('items')
                if not isinstance(page, list):
                    raise ServiceError(f'{path}: no items at {offset}')
                total = body.get('totalNumberOfItems', total)
                items += page
                offset += len(page)
                if not page or not isinstance(total, int) or offset >= total:
                    break
        if isinstance(total, int) and len(items) < min(total, MAX_ITEMS):
            raise ServiceError(f'{path}: got {len(items)} of {total} items')
        name = info.get('title') or f'Tidal {kind}'
        if kind == 'album':
            owner = '; '.join(_artists(info))
            tracks = [t for t in items if isinstance(t, dict)]
        else:
            owner = (info.get('creator') or {}).get('name') or ('TIDAL' if info.get('type') == 'EDITORIAL' else '')
            tracks = [i.get('item') or {} for i in items if isinstance(i, dict) and i.get('type') == 'track']
        rows = [row(_title(t), _artists(t), (t.get('album') or {}).get('title') or (name if kind == 'album' else ''),
                    (t.get('duration') or 0) * 1000, t.get('isrc'), t.get('url'))
                for t in tracks[:MAX_ITEMS] if t.get('title')]
        result = Playlist(xid, name, owner, rows)
        expected = info.get('numberOfTracks')
        if isinstance(expected, int):
            result.unavailable = max(0, min(expected, MAX_ITEMS) - len(rows))
        log.info('tidal %s %s: %r, %d songs (%d unavailable)', kind, xid, name, len(rows), result.unavailable)
        return result

    async def song(self, xid):
        async with self._session() as http:
            t = await self._get(http, f'tracks/{xid}', 'song')
        return Song(_title(t), '; '.join(_artists(t)), (t.get('album') or {}).get('title') or '',
                    t.get('duration') or None)
