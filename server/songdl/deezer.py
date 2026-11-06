"""Reads Deezer playlist, album and song links through Deezer's public API
(api.deezer.com), which needs no sign-in.

A playlist's track list is paged by `next` links. Songs Deezer no longer
has are left out of those pages without a word, so the playlist's own count
(nb_tracks) says how many are missing; they are reported as unavailable,
like a YouTube playlist's deleted videos.
"""

import asyncio
import logging
import re

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

API = 'https://api.deezer.com'
PAGE = 400            # songs per request; Deezer gives up to about 1000
MAX_ITEMS = 10_000
TIMEOUT = 20
RETRIES = 4

# deezer.com/playlist/<id>, with or without a language (/us/, /en/, /pt-br/)
LINK = re.compile(r'\bdeezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(playlist|album|track)/(\d+)', re.I)
KINDS = {'playlist': 'playlist', 'album': 'album', 'track': 'song'}

NOT_FOUND = {
    'playlist': 'Deezer couldn\'t find that playlist. Check the link, and that the playlist isn\'t private.',
    'album': 'Deezer couldn\'t find that album. Check the link.',
    'song': 'Deezer couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, id) for a Deezer playlist, album or song link, or None."""
    m = LINK.search(str(text or ''))
    return (KINDS[m.group(1).lower()], m.group(2)) if m else None


def _artist(item):
    return ((item or {}).get('artist') or {}).get('name') or ''


def _row(item, album=''):
    return row(item.get('title') or item.get('title_short'), [_artist(item)],
               album or ((item.get('album') or {}).get('title')),
               (item.get('duration') or 0) * 1000, item.get('isrc'), item.get('link'))


class Deezer:
    name = 'Deezer'

    async def _get(self, http, url, kind, params=None):
        for attempt in range(RETRIES):
            async with http.get(url, params=params) as res:
                if res.status >= 500 or res.status == 429:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                try:
                    body = await res.json(content_type=None)
                except ValueError:
                    raise ServiceError(f'{url} answered {res.status}, not JSON')
            error = body.get('error') if isinstance(body, dict) else None
            if not error:
                if not isinstance(body, dict):
                    raise ServiceError(f'{url}: unexpected answer')
                return body
            if error.get('code') == 4:     # "Quota limit exceeded": 50 requests per 5 seconds
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            if error.get('code') in (800, 200, 300) or error.get('type') in ('DataException', 'OAuthException',
                                                                             'PermissionException'):
                # 800: no such thing; the others answer a private playlist
                log.info('deezer %s: %s', url, error)
                raise UserError(NOT_FOUND[kind], 404)
            raise ServiceError(f'{url}: {error}')
        raise ServiceError(f'{url}: gave up after {RETRIES} tries')

    def _session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                     headers={'User-Agent': USER_AGENT, 'Accept-Language': 'en'})

    async def _pages(self, http, url, kind):
        items, params = [], {'index': 0, 'limit': PAGE}
        while url and len(items) < MAX_ITEMS:
            body = await self._get(http, url, kind, params)
            data = body.get('data')
            if not isinstance(data, list):
                raise ServiceError(f'{url}: no data in the page')
            items += data
            nxt = body.get('next')
            if not nxt or not data:
                break
            url, params = nxt, None    # the next link carries its own index and limit
        return items[:MAX_ITEMS]

    async def read(self, kind, xid):
        return await (self.song(xid) if kind == 'song' else self.playlist(kind, xid))

    async def playlist(self, kind, pid):
        async with self._session() as http:
            info = await self._get(http, f'{API}/{kind}/{pid}', kind)
            items = await self._pages(http, f'{API}/{kind}/{pid}/tracks', kind)
        album = info.get('title') if kind == 'album' else ''
        rows = [_row(i, album) for i in items if i.get('title')]
        expected = info.get('nb_tracks')
        if kind == 'album':
            owner = _artist(info)
        else:
            owner = (info.get('creator') or {}).get('name') or ''
        name = info.get('title') or f'Deezer {kind}'
        result = Playlist(str(pid), name, owner, rows)
        if isinstance(expected, int):
            result.unavailable = max(0, min(expected, MAX_ITEMS) - len(rows))
        log.info('deezer %s %s: %r, %d songs (%d unavailable)', kind, pid, name, len(rows), result.unavailable)
        return result

    async def song(self, tid):
        async with self._session() as http:
            item = await self._get(http, f'{API}/track/{tid}', 'song')
        artists = [c.get('name') for c in item.get('contributors') or [] if c.get('role', 'Main') == 'Main']
        return Song(item.get('title') or '', '; '.join(artists or [_artist(item)]),
                    (item.get('album') or {}).get('title') or '', item.get('duration') or None)
