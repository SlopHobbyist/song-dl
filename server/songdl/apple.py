"""Reads Apple Music playlist, album and song links.

Apple's catalog API (amp-api.music.apple.com) is what the music.apple.com web
player itself reads, with a developer token that its main script carries for
every visitor (a JWT issued by "AMPWebPlay", good for a couple of months).
So: fetch a music.apple.com page, find its index~<hash>.js, take the token
out of it, and send it with Origin: https://music.apple.com (without that
Origin the API answers 401). A 401 later means Apple has moved on to a new
token, which is fetched again.

Track lists are paged 300 at a time until an answer has no `next`. The
playlist's own page stops at 100 songs and the album's at 300, so neither is
used for the list. Each link is read in its own country's catalog (the "us" in
music.apple.com/us/...): another country may not have every song.
"""

import asyncio
import base64
import json
import logging
import re
import time
from urllib.parse import parse_qs

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

SITE = 'https://music.apple.com'
API = 'https://amp-api.music.apple.com/v1/catalog/{sf}/'
PAGE = 300            # the most a /tracks page gives
MAX_ITEMS = 10_000
TIMEOUT = 30
RETRIES = 4

# music.apple.com/<country>/playlist/<name>/pl.<id>, /album/<name>/<id>, /album/<name>/<id>?i=<song>,
# /song/<name>/<id>; the name and country may be missing. Also embed., geo. and itunes.apple.com.
LINK = re.compile(
    r'\b(?:(?:embed|geo|beta|classical)\.)?(?:music|itunes)\.apple\.com/(?:(?P<sf>[a-z]{2})/)?'
    r'(?P<kind>playlist|album|song)/(?:[^/?#\s]+/)?(?P<id>pl\.(?:u-)?[A-Za-z0-9-]+|(?:id)?\d+)\b/?(?P<query>\?\S*)?',
    re.I)
BUNDLE = re.compile(r'src="(/assets/index[^"]*\.js)"')
JWT = re.compile(r'eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+')

PATHS = {'playlist': 'playlists', 'album': 'albums', 'song': 'songs'}
NOT_FOUND = {
    'playlist': 'Apple Music couldn\'t find that playlist. Check the link, and that the playlist is shared: '
                'Song-DL can only read playlists that are public or shared by link.',
    'album': 'Apple Music couldn\'t find that album. Check the link.',
    'song': 'Apple Music couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, "<country>/<id>") for an Apple Music link, or None."""
    m = LINK.search(str(text or ''))
    if not m:
        return None
    sf, kind, ident = (m.group('sf') or 'us').lower(), m.group('kind').lower(), m.group('id')
    if kind == 'playlist':
        if not ident.startswith('pl.'):
            return None
    else:
        ident = ident.lower().removeprefix('id')
        if not ident.isdigit():
            return None
    if kind == 'album':
        song = parse_qs((m.group('query') or '?')[1:]).get('i', [''])[0]
        if song.isdigit():
            return 'song', f'{sf}/{song}'
    return kind, f'{sf}/{ident}'


def _payload(token):
    part = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


def _artists(item):
    rel = (((item.get('relationships') or {}).get('artists') or {}).get('data')) or []
    names = [((a.get('attributes') or {}).get('name')) for a in rel]
    return [n for n in names if n] or [(item.get('attributes') or {}).get('artistName') or '']


class AppleMusic:
    name = 'Apple Music'

    def __init__(self):
        self.token = None
        self.token_expires = 0.0
        self.lock = asyncio.Lock()

    def _session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                     headers={'User-Agent': USER_AGENT})

    async def _token(self, http):
        async with self.lock:
            if self.token and time.time() < self.token_expires - 3600:
                return self.token
            async with http.get(f'{SITE}/us/browse') as res:
                html = await res.text()
            m = BUNDLE.search(html)
            if not m:
                raise ServiceError('no web player script in the page')
            async with http.get(SITE + m.group(1)) as res:
                script = await res.text()
            best = None
            for token in dict.fromkeys(JWT.findall(script)):
                try:
                    claims = _payload(token)
                except (ValueError, IndexError):
                    continue
                if claims.get('exp', 0) < time.time():
                    continue
                if claims.get('iss') == 'AMPWebPlay':
                    best = (token, claims['exp'])
                    break
                best = best or (token, claims['exp'])
            if not best:
                raise ServiceError('no token in the web player script')
            self.token, self.token_expires = best
            log.info('apple music: new web player token, good until %s',
                     time.strftime('%Y-%m-%d', time.gmtime(self.token_expires)))
            return self.token

    async def _get(self, http, url, params=None):
        """(status, body) for an API address; 404 comes back for the caller to word."""
        for attempt in range(RETRIES):
            token = await self._token(http)
            headers = {'Authorization': f'Bearer {token}', 'Origin': SITE, 'Accept': 'application/json'}
            async with http.get(url, params=params, headers=headers) as res:
                if res.status == 401:
                    self.token = None     # Apple has moved on to a new token
                    continue
                if res.status == 429 or res.status >= 500:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                try:
                    body = await res.json(content_type=None)
                except ValueError:
                    body = None
                if res.status in (200, 404):
                    return res.status, body
                if res.status == 400 and 'storefront' in json.dumps(body or '').lower():
                    raise UserError('Apple Music doesn\'t know the country in that link.')
                raise ServiceError(f'{url} answered {res.status}: {json.dumps(body)[:200]}')
        raise ServiceError(f'{url}: gave up after {RETRIES} tries')

    async def _thing(self, http, kind, ref):
        sf, ident = ref.split('/', 1)
        status, body = await self._get(http, API.format(sf=sf) + f'{PATHS[kind]}/{ident}', {'include': 'artists'})
        if status == 404:
            raise UserError(NOT_FOUND[kind], 404)
        try:
            return body['data'][0]
        except (KeyError, IndexError, TypeError):
            raise ServiceError(f'no {kind} in the answer')

    async def read(self, kind, xid):
        return await (self.song(xid) if kind == 'song' else self.playlist(kind, xid))

    async def playlist(self, kind, ref):
        sf, ident = ref.split('/', 1)
        async with self._session() as http:
            info = await self._thing(http, kind, ref)
            url = API.format(sf=sf) + f'{PATHS[kind]}/{ident}/tracks'
            items, offset = [], 0
            while len(items) < MAX_ITEMS:
                status, body = await self._get(http, url, {'limit': PAGE, 'offset': offset, 'include': 'artists'})
                if status == 404 and offset == 0:
                    break      # an empty playlist: "No related resources"
                if status != 200 or not isinstance((body or {}).get('data'), list):
                    raise ServiceError(f'track page at {offset} answered {status}')
                items += body['data']
                nxt = body.get('next')
                if not nxt or not body['data']:
                    break
                m = re.search(r'[?&]offset=(\d+)', nxt)
                offset = int(m.group(1)) if m else offset + len(body['data'])
        attrs = info.get('attributes') or {}
        name = attrs.get('name') or f'Apple Music {kind}'
        owner = attrs.get('curatorName') if kind == 'playlist' else attrs.get('artistName')
        rows = []
        for item in items[:MAX_ITEMS]:
            a = item.get('attributes') or {}
            if a.get('name'):
                rows.append(row(a['name'], _artists(item), a.get('albumName'), a.get('durationInMillis'),
                                a.get('isrc'), a.get('url')))
        result = Playlist(ident, name, owner or '', rows)
        if kind == 'album' and isinstance(attrs.get('trackCount'), int):
            result.unavailable = max(0, attrs['trackCount'] - len(rows))
        log.info('apple music %s %s: %r, %d songs', kind, ref, name, len(rows))
        return result

    async def song(self, ref):
        async with self._session() as http:
            item = await self._thing(http, 'song', ref)
        a = item.get('attributes') or {}
        ms = a.get('durationInMillis')
        return Song(a.get('name') or '', '; '.join(_artists(item)), a.get('albumName') or '',
                    round(ms / 1000) if ms else None)
