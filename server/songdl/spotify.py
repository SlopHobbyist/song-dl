"""Turns a Spotify playlist link into a CSV of every song on it.

Spotify's own Web API wants a developer app, and the playlist's embed page
(what most link-to-CSV sites read) stops at 100 songs. The web player reads
playlists through a GraphQL endpoint ("pathfinder") that pages through all of
them, and the embed page hands out an anonymous token that works with it. So:

  1. GET the playlist's embed page and take the token from its __NEXT_DATA__.
  2. Ask pathfinder's fetchPlaylist for the songs, 100 at a time, until the
     count matches the playlist's totalCount. A playlist that comes back short
     is an error, never a shorter CSV.

fetchPlaylist is a persisted query: it is called by the hash of its text, and
the hash changes when Spotify updates the web player. Known hashes are tried
first; if Spotify no longer knows any of them, the current one is read out of
the web player's script.

Public playlists and ones left off the owner's profile both work: anyone with
the link can read them. Private playlists can't be read without signing in as
their owner.
"""

import asyncio
import csv
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp

from .errors import UserError

log = logging.getLogger('songdl')

EMBED_URL = 'https://open.spotify.com/embed/playlist/{id}'
QUERY_URL = 'https://api-partner.spotify.com/pathfinder/v1/query'
PLAYER_URL = 'https://open.spotify.com/'
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/130.0 Safari/537.36')

# Newest first. A hash read from the web player is put in front of these.
HASHES = ['21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf',
          '19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d']
PAGE = 100            # songs per request (the most pathfinder returns)
PARALLEL = 4          # requests at once once the size is known
MAX_ITEMS = 10_000    # Spotify's own limit on a playlist
TIMEOUT = 20
RETRIES = 4

# open.spotify.com/playlist/<id>, also with /intl-xx/, /embed/ or /user/<name>/ in
# front, and spotify:playlist:<id> / spotify:user:<name>:playlist:<id>
PLAYLIST = re.compile(
    r'(?:\bopen\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(?:user/[^/\s]+/)?playlist/'
    r'|\bspotify:(?:user:[^:\s]+:)?playlist:)([A-Za-z0-9]{22})\b')
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SCRIPT = re.compile(r'https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[\w.]+\.js')
HASH_IN_SCRIPT = re.compile(r'"fetchPlaylist","query","([0-9a-f]{64})"')

COLUMNS = ['Track URI', 'Track Name', 'Artist Name(s)', 'Album Name', 'Duration (ms)', 'Added At']


def playlist_id(text):
    """The playlist ID in a Spotify link or URI, or None."""
    m = PLAYLIST.search(str(text or ''))
    return m.group(1) if m else None


class SpotifyError(Exception):
    """Spotify answered in a way this module doesn't understand."""


class _StaleHash(Exception):
    pass


@dataclass
class Playlist:
    id: str
    name: str
    owner: str
    rows: list = field(default_factory=list)   # one dict per item, keyed by COLUMNS

    def csv(self):
        out = io.StringIO()
        writer = csv.DictWriter(out, COLUMNS, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(self.rows)
        # The BOM makes Excel read the file as UTF-8
        return '﻿' + out.getvalue()


class Spotify:
    def __init__(self):
        self.token = None
        self.token_expires = 0.0
        self.hashes = list(HASHES)
        self.lock = asyncio.Lock()

    async def playlist(self, pid):
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': USER_AGENT}) as http:
            try:
                return await self._playlist(http, pid)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning('spotify playlist %s: %r', pid, e)
                raise UserError('Couldn\'t reach Spotify. Please try again in a minute.', 502)
            except SpotifyError as e:
                log.warning('spotify playlist %s: %s', pid, e)
                raise UserError('Spotify answered in a way Song-DL doesn\'t understand, so the playlist '
                                'couldn\'t be read. Try again later, or export it to CSV another way.', 502)

    async def _playlist(self, http, pid):
        first = await self._page(http, pid, 0)
        total = min(first['content']['totalCount'], MAX_ITEMS)
        pages = {0: first['content']['items']}

        sem = asyncio.Semaphore(PARALLEL)

        async def fetch(offset):
            async with sem:
                pages[offset] = (await self._page(http, pid, offset))['content']['items']

        await asyncio.gather(*(fetch(o) for o in range(len(pages[0]), total, PAGE)))
        items = []
        for offset in sorted(pages):
            if offset != len(items):
                break      # a page came back short, leaving a gap
            items += pages[offset]
        if len(items) < total:
            # A shorter CSV would quietly leave songs out, so refuse instead
            raise SpotifyError(f'got {len(items)} of {total} items')
        items = items[:total]

        owner = ((first.get('ownerV2') or {}).get('data') or {}).get('name') or ''
        result = Playlist(pid, first.get('name') or 'Spotify playlist', owner)
        result.rows = [_row(item) for item in items]
        log.info('spotify playlist %s: %r, %d songs', pid, result.name, len(result.rows))
        return result

    async def _page(self, http, pid, offset):
        """One page of the playlist: playlistV2 with content.items from `offset`."""
        for attempt in range(RETRIES):
            token = await self._token(http, pid)
            for h in list(self.hashes):
                try:
                    status, body = await self._query(http, token, pid, offset, h)
                except _StaleHash:
                    continue
                if status == 401:
                    self.token = None
                    break
                if status == 429 or status >= 500:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    break
                return _playlist_v2(body)
            else:
                # Spotify knows none of the hashes: find the web player's current one
                await self._discover_hash(http)
        raise SpotifyError(f'gave up after {RETRIES} tries at offset {offset}')

    async def _query(self, http, token, pid, offset, sha):
        variables = {'uri': f'spotify:playlist:{pid}', 'offset': offset, 'limit': PAGE,
                     'enableWatchFeedEntrypoint': False}
        params = {'operationName': 'fetchPlaylist', 'variables': json.dumps(variables),
                  'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': sha}})}
        headers = {'Authorization': f'Bearer {token}', 'app-platform': 'WebPlayer', 'Accept': 'application/json'}
        async with http.get(QUERY_URL, params=params, headers=headers) as res:
            try:
                body = await res.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                body = None
            errors = body.get('errors') if isinstance(body, dict) else None
            if errors and not (body.get('data') or {}).get('playlistV2'):
                text = json.dumps(errors)[:300]
                # The hash is unknown, or the query behind it wants variables this doesn't send
                if 'PersistedQueryNotFound' in text or 'VALIDATION' in text or 'variable' in text:
                    log.info('spotify: query hash %s… no longer works: %s', sha[:8], text)
                    raise _StaleHash()
                raise SpotifyError(f'query errors: {text}')
            if res.status == 200 and not isinstance(body, dict):
                raise SpotifyError('query answer was not JSON')
            return res.status, body

    async def _token(self, http, pid):
        async with self.lock:
            if self.token and time.time() < self.token_expires - 60:
                return self.token
            async with http.get(EMBED_URL.format(id=pid)) as res:
                if res.status == 404:
                    raise UserError(NOT_FOUND, 404)
                if res.status != 200:
                    raise SpotifyError(f'embed page answered {res.status}')
                html = await res.text()
            m = NEXT_DATA.search(html)
            try:
                data = json.loads(m.group(1))
                session = data['props']['pageProps']['state']['settings']['session']
                self.token = session['accessToken']
                self.token_expires = session.get('accessTokenExpirationTimestampMs', 0) / 1000 or time.time() + 600
            except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
                raise SpotifyError('no token in the embed page')
            return self.token

    async def _discover_hash(self, http):
        async with http.get(PLAYER_URL) as res:
            html = await res.text()
        for url in dict.fromkeys(SCRIPT.findall(html)):
            async with http.get(url) as res:
                m = HASH_IN_SCRIPT.search(await res.text())
            if m:
                if m.group(1) in self.hashes:
                    break
                log.info('spotify: using fetchPlaylist hash %s… from the web player', m.group(1)[:8])
                self.hashes.insert(0, m.group(1))
                return
        raise SpotifyError('couldn\'t find a working fetchPlaylist hash')


NOT_FOUND = ('Spotify couldn\'t find that playlist. Check the link, and that the playlist isn\'t '
             'private: Song-DL can only read public playlists, or ones shared by link.')


def _playlist_v2(body):
    pl = ((body or {}).get('data') or {}).get('playlistV2')
    if not isinstance(pl, dict):
        raise SpotifyError('no playlistV2 in the answer')
    kind = pl.get('__typename')
    if kind != 'Playlist':
        # NotFound, or something like Forbidden for a private playlist
        log.info('spotify: playlist is %s: %s', kind, pl.get('message'))
        raise UserError(NOT_FOUND, 404)
    content = pl.get('content') or {}
    if not isinstance(content.get('items'), list) or not isinstance(content.get('totalCount'), int):
        raise SpotifyError('no items in the answer')
    return pl


def _names(artists):
    return [((a or {}).get('profile') or {}).get('name') or '' for a in ((artists or {}).get('items') or [])]


def _row(item):
    """A CSV row for one playlist item: a song, a podcast episode or a local file."""
    data = ((item or {}).get('itemV2') or {}).get('data') or {}
    kind = data.get('__typename')
    if kind == 'Episode':
        show = ((data.get('podcastV2') or {}).get('data') or {}).get('name') or ''
        artists, album = [show], show
    elif kind == 'LocalTrack':
        artists = [(data.get('artist') or {}).get('name') or data.get('artistName') or '']
        album = (data.get('album') or {}).get('name') or data.get('albumName') or ''
    else:
        artists = _names(data.get('artists'))
        album = (data.get('albumOfTrack') or {}).get('name') or ''
    duration = ((data.get('trackDuration') or data.get('duration') or {}).get('totalMilliseconds')) or ''
    return {
        'Track URI': data.get('uri') or '',
        'Track Name': data.get('name') or '',
        # ";" between artists, so names with a comma of their own stay whole
        'Artist Name(s)': '; '.join(a for a in artists if a),
        'Album Name': album,
        'Duration (ms)': duration,
        'Added At': ((item or {}).get('addedAt') or {}).get('isoString') or '',
    }
