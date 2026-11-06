"""Reads Beatport track, release and chart links.

www.beatport.com is behind a bot check, but Beatport's API (api.beatport.com/v4)
isn't. Beatport's embeddable player signs in to it with an OAuth client whose
id and secret are in its script; a client_credentials grant with them gives a
token good for ten minutes, which reads tracks, releases and charts. The pair
is kept here, and read out of the embed player's script again if Beatport
ever turns it down.

Lists are paged 100 at a time (the most Beatport gives) by `next` links. A
chart is read like a playlist, a release like an album.

Beatport's own playlists (beatport.com/playlists/share/…) can only be read
with a token that the bot-checked site hands out, so they aren't supported.
"""

import asyncio
import logging
import re
import time

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

API = 'https://api.beatport.com/v4/catalog/'
TOKEN_URL = 'https://account.beatport.com/o/token/'
EMBED = 'https://embed.beatport.com'
CLIENT = ('2tiTbKxmQFwnbFjMONU4k7njMRZmV3ZMwRBndiZs',   # the embed player's, seen 2026-11-06
          'RDUJyAk4zFEGtQ8rsTmylDSfxmALRNBn3D1BsRr7MKi3oa1TL9Mq9QxqUPK7loiumXolEWbJcWa4IGAhtwnTz1c'
          'SXClGJ1tkkNCNWwRwjxIKTZJKOJxbwaNt0Rm3WG0v')
PAGE = 100
MAX_ITEMS = 10_000
TIMEOUT = 30
RETRIES = 4

# beatport.com/track/<name>/<id>, /release/<name>/<id>, /chart/<name>/<id>, with or without /de/ in front
LINK = re.compile(r'\bbeatport\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(track|release|chart)/[^/?#\s]+/(\d+)\b', re.I)
KINDS = {'track': 'song', 'release': 'album', 'chart': 'playlist'}
PATHS = {'song': 'tracks', 'album': 'releases', 'playlist': 'charts'}
UNSUPPORTED = [(re.compile(r'\bbeatport\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?playlists?/', re.I),
                'Beatport playlists can\'t be read, only Beatport charts, releases and tracks. '
                'Export the playlist to CSV another way, or paste a chart link.')]
INDEX_SCRIPT = re.compile(r'src="(/assets/index[^"]*\.js)"')
CLIENT_IN_SCRIPT = re.compile(r'client_id:"([A-Za-z0-9]+)",client_secret:"([A-Za-z0-9]+)"')

NOT_FOUND = {
    'playlist': 'Beatport couldn\'t find that chart. Check the link.',
    'album': 'Beatport couldn\'t find that release. Check the link.',
    'song': 'Beatport couldn\'t find that track. Check the link.',
}


def find(text):
    """(kind, id) for a Beatport chart, release or track link, or None."""
    m = LINK.search(str(text or ''))
    return (KINDS[m.group(1).lower()], m.group(2)) if m else None


def _title(t):
    """"Name (Mix Name)", as Beatport shows it, unless the name already ends with the mix."""
    name, mix = t.get('name') or '', (t.get('mix_name') or '').strip()
    return name if not mix or name.lower().endswith(f'({mix.lower()})') else f'{name} ({mix})'


def _artists(t):
    names = [a.get('name') for a in t.get('artists') or [] if isinstance(a, dict)]
    return [n for n in names if n]


class Beatport:
    name = 'Beatport'

    def __init__(self):
        self.client = CLIENT
        self.token = None
        self.token_expires = 0.0
        self.lock = asyncio.Lock()

    async def read(self, kind, xid):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                         headers={'User-Agent': USER_AGENT}) as http:
            if kind == 'song':
                t = await self._get(http, f'{API}tracks/{xid}/', kind)
                return Song(_title(t), '; '.join(_artists(t)), (t.get('release') or {}).get('name') or '',
                            round(t['length_ms'] / 1000) if t.get('length_ms') else None)
            return await self._list(http, kind, xid)

    async def _grant(self, http, client):
        async with http.post(TOKEN_URL, data={'grant_type': 'client_credentials', 'client_id': client[0],
                                              'client_secret': client[1]}) as res:
            body = await res.json(content_type=None) if res.status == 200 else None
        if not isinstance(body, dict) or not body.get('access_token'):
            return None
        return body['access_token'], time.time() + int(body.get('expires_in') or 600)

    async def _scrape_client(self, http):
        async with http.get(EMBED + '/') as res:
            m = INDEX_SCRIPT.search(await res.text())
        if not m:
            return None
        async with http.get(EMBED + m.group(1)) as res:
            m = CLIENT_IN_SCRIPT.search(await res.text())
        return (m.group(1), m.group(2)) if m else None

    async def _token(self, http):
        async with self.lock:
            if not self.token or time.time() > self.token_expires - 30:
                got = await self._grant(http, self.client)
                if not got:
                    client = await self._scrape_client(http)
                    if client and client != self.client:
                        log.info('beatport: using the embed player\'s new client %s', client[0])
                        self.client = client
                        got = await self._grant(http, client)
                if not got:
                    raise ServiceError('no token for the embed player\'s client')
                self.token, self.token_expires = got
            return self.token

    async def _get(self, http, url, kind):
        for attempt in range(RETRIES):
            headers = {'Authorization': f'Bearer {await self._token(http)}', 'Accept': 'application/json'}
            async with http.get(url, headers=headers) as res:
                try:
                    body = await res.json(content_type=None)
                except ValueError:
                    body = None
                status = res.status
            if status == 200 and isinstance(body, dict):
                return body
            if status == 404:
                raise UserError(NOT_FOUND[kind], 404)
            if status in (401, 403) and attempt == 0:
                self.token = None
                continue
            if status == 429 or status >= 500:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise ServiceError(f'{url} answered {status}: {str(body)[:200]}')
        raise ServiceError(f'{url}: gave up after {RETRIES} tries')

    async def _list(self, http, kind, xid):
        base = f'{API}{PATHS[kind]}/{xid}/'
        info = await self._get(http, base, kind)
        items, url = [], f'{base}tracks/?page=1&per_page={PAGE}'
        while url and len(items) < MAX_ITEMS:
            body = await self._get(http, url, kind)
            page = body.get('results')
            if not isinstance(page, list):
                raise ServiceError(f'{url}: no results')
            items += page
            url = body.get('next')
            if url and not url.startswith('http'):
                url = 'https://' + url
            if not page:
                break
        total = info.get('track_count')
        if isinstance(total, int) and len(items) < min(total, MAX_ITEMS):
            raise ServiceError(f'{base}: got {len(items)} of {total} tracks')
        name = info.get('name') or f'Beatport {kind}'
        if kind == 'album':
            artists = _artists(info)
            # A compilation names every artist on it: the label says more
            owner = ('; '.join(artists) if 0 < len(artists) <= 3 else '') or (info.get('label') or {}).get('name') or ''
        else:
            owner = (info.get('person') or {}).get('owner_name') or (info.get('artist') or {}).get('name') or ''
        rows = [row(_title(t), _artists(t), (t.get('release') or {}).get('name') or (name if kind == 'album' else ''),
                    t.get('length_ms'), t.get('isrc'), t.get('url') or '')
                for t in items[:MAX_ITEMS] if isinstance(t, dict) and t.get('name')]
        result = Playlist(xid, name, owner, rows)
        log.info('beatport %s %s: %r, %d tracks', kind, xid, name, len(rows))
        return result
