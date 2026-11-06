"""Turns a Spotify playlist or album link into a CSV of every song on it, and a
song link into the song to search for.

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

Albums are read the same way, through getAlbum (50 songs a page). A song
link only needs the song's embed page, which names it and its artists.

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

from .catalog import Song
from .errors import UserError

log = logging.getLogger('songdl')

EMBED_URL = 'https://open.spotify.com/embed/{kind}/{id}'
QUERY_URL = 'https://api-partner.spotify.com/pathfinder/v1/query'
PLAYER_URL = 'https://open.spotify.com/'
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/130.0 Safari/537.36')

# Per query, newest first. A hash read from the web player is put in front of these.
HASHES = {
    'fetchPlaylist': ['21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf',
                      '19ff1327c29e99c208c86d7a9d8f1929cfdf3d3202a0ff4253c821f1901aa94d'],
    'getAlbum': ['0b6ce3511e1b02e4a40fa95440f992acbe85080eb308af9194322d708fc1914f'],
}
OPERATION = {'playlist': 'fetchPlaylist', 'album': 'getAlbum'}
PAGES = {'playlist': 100, 'album': 50}   # songs per request (what the web player asks for)
PARALLEL = 4          # requests at once once the size is known
MAX_ITEMS = 10_000    # Spotify's own limit on a playlist
TIMEOUT = 20
RETRIES = 4

# open.spotify.com/playlist/<id>, also with /intl-xx/, /embed/ or /user/<name>/ in
# front, and spotify:playlist:<id> / spotify:user:<name>:playlist:<id>
PLAYLIST = re.compile(
    r'(?:\bopen\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(?:user/[^/\s]+/)?playlist/'
    r'|\bspotify:(?:user:[^:\s]+:)?playlist:)([A-Za-z0-9]{22})\b')
# open.spotify.com/album/<id> and /track/<id> (also /intl-xx/ and /embed/), spotify:album:<id>
OTHER = re.compile(r'(?:\b(?:open|play)\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(album|track)/'
                   r'|\bspotify:(album|track):)([A-Za-z0-9]{22})\b')
KINDS = {'album': 'album', 'track': 'song'}
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
SCRIPT = re.compile(r'https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[\w.]+\.js')
HASH_IN_SCRIPT = '"{op}","query","([0-9a-f]{{64}})"'

COLUMNS = ['Track URI', 'Track Name', 'Artist Name(s)', 'Album Name', 'Duration (ms)', 'Added At']


def playlist_id(text):
    """The playlist ID in a Spotify link or URI, or None."""
    m = PLAYLIST.search(str(text or ''))
    return m.group(1) if m else None


def find(text):
    """(kind, id) for a Spotify playlist, album or song link, or None."""
    if pid := playlist_id(text):
        return 'playlist', pid
    m = OTHER.search(str(text or ''))
    return (KINDS[m.group(1) or m.group(2)], m.group(3)) if m else None


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
    name = 'Spotify'

    def __init__(self):
        self.token = None
        self.token_expires = 0.0
        self.hashes = {op: list(h) for op, h in HASHES.items()}
        self.lock = asyncio.Lock()

    async def read(self, kind, xid):
        return await (self.song(xid) if kind == 'song' else self.playlist(xid, kind))

    async def playlist(self, pid, kind='playlist'):
        """A playlist's songs, or with kind='album' an album's."""
        return await self._run(kind, pid, self._playlist(kind, pid))

    async def album(self, aid):
        return await self.playlist(aid, 'album')

    async def song(self, tid):
        return await self._run('song', tid, self._song(tid))

    async def _run(self, kind, xid, work):
        try:
            return await work
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning('spotify %s %s: %r', kind, xid, e)
            raise UserError('Couldn\'t reach Spotify. Please try again in a minute.', 502)
        except SpotifyError as e:
            log.warning('spotify %s %s: %s', kind, xid, e)
            raise UserError(f'Spotify answered in a way Song-DL doesn\'t understand, so the {kind} '
                            'couldn\'t be read. Try again later, or export it to CSV another way.', 502)

    def _session(self):
        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        return aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': USER_AGENT})

    async def _playlist(self, kind, pid):
        async with self._session() as http:
            return await self._read(http, kind, pid)

    async def _read(self, http, kind, pid):
        page = PAGES[kind]
        first = await self._page(http, kind, pid, 0)
        total = min(first['content']['totalCount'], MAX_ITEMS)
        pages = {0: first['content']['items']}

        sem = asyncio.Semaphore(PARALLEL)

        async def fetch(offset):
            async with sem:
                pages[offset] = (await self._page(http, kind, pid, offset))['content']['items']

        await asyncio.gather(*(fetch(o) for o in range(len(pages[0]), total, page)))
        items = []
        for offset in sorted(pages):
            if offset != len(items):
                break      # a page came back short, leaving a gap
            items += pages[offset]
        if len(items) < total:
            # A shorter CSV would quietly leave songs out, so refuse instead
            raise SpotifyError(f'got {len(items)} of {total} items')
        items = items[:total]

        if kind == 'album':
            owner = '; '.join(a for a in _names(first.get('artists')) if a)
            album = first.get('name') or ''
            result = Playlist(pid, album or 'Spotify album', owner)
            result.rows = [_row({'itemV2': {'data': {**(item.get('track') or {}), 'albumOfTrack': {'name': album}}}})
                           for item in items]
        else:
            owner = ((first.get('ownerV2') or {}).get('data') or {}).get('name') or ''
            result = Playlist(pid, first.get('name') or 'Spotify playlist', owner)
            result.rows = [_row(item) for item in items]
        log.info('spotify %s %s: %r, %d songs', kind, pid, result.name, len(result.rows))
        return result

    async def _song(self, tid):
        async with self._session() as http:
            entity = (await self._embed(http, 'track', tid))['props']['pageProps']['state']['data']['entity']
        try:
            artists = [a.get('name') or '' for a in entity.get('artists') or []]
            ms = entity.get('duration')
            return Song(entity.get('name') or entity['title'], '; '.join(a for a in artists if a), '',
                        round(ms / 1000) if isinstance(ms, (int, float)) and ms > 0 else None)
        except (AttributeError, KeyError, TypeError):
            raise SpotifyError('no song in the embed page')

    async def _page(self, http, kind, pid, offset):
        """One page of a playlist or album, with content.items from `offset`."""
        op = OPERATION[kind]
        for attempt in range(RETRIES):
            token = await self._token(http, kind, pid)
            for h in list(self.hashes[op]):
                try:
                    status, body = await self._query(http, token, kind, pid, offset, h)
                except _StaleHash:
                    continue
                if status == 401:
                    self.token = None
                    break
                if status == 429 or status >= 500:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    break
                return _album(body) if kind == 'album' else _playlist_v2(body)
            else:
                # Spotify knows none of the hashes: find the web player's current one
                await self._discover_hash(http, op)
        raise SpotifyError(f'gave up after {RETRIES} tries at offset {offset}')

    async def _query(self, http, token, kind, pid, offset, sha):
        op = OPERATION[kind]
        if kind == 'album':
            variables = {'uri': f'spotify:album:{pid}', 'locale': '', 'offset': offset, 'limit': PAGES[kind]}
        else:
            variables = {'uri': f'spotify:playlist:{pid}', 'offset': offset, 'limit': PAGES[kind],
                         'enableWatchFeedEntrypoint': False}
        params = {'operationName': op, 'variables': json.dumps(variables),
                  'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': sha}})}
        headers = {'Authorization': f'Bearer {token}', 'app-platform': 'WebPlayer', 'Accept': 'application/json'}
        async with http.get(QUERY_URL, params=params, headers=headers) as res:
            try:
                body = await res.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError):
                body = None
            errors = body.get('errors') if isinstance(body, dict) else None
            if errors and not (body.get('data') or {}).get('albumUnion' if kind == 'album' else 'playlistV2'):
                text = json.dumps(errors)[:300]
                # The hash is unknown, or the query behind it wants variables this doesn't send
                if 'PersistedQueryNotFound' in text or 'VALIDATION' in text or 'variable' in text:
                    log.info('spotify: query hash %s… no longer works: %s', sha[:8], text)
                    raise _StaleHash()
                raise SpotifyError(f'query errors: {text}')
            if res.status == 200 and not isinstance(body, dict):
                raise SpotifyError('query answer was not JSON')
            return res.status, body

    async def _token(self, http, kind, pid):
        async with self.lock:
            if self.token and time.time() < self.token_expires - 60:
                return self.token
            await self._embed(http, kind, pid)
            return self.token

    async def _embed(self, http, kind, xid):
        """The embed page's data for a playlist, album or track; it also holds a fresh token."""
        async with http.get(EMBED_URL.format(kind=kind, id=xid)) as res:
            if res.status == 404:
                raise UserError(NOT_FOUND[KINDS.get(kind, kind)], 404)
            if res.status != 200:
                raise SpotifyError(f'embed page answered {res.status}')
            html = await res.text()
        m = NEXT_DATA.search(html)
        try:
            data = json.loads(m.group(1))
            if data['props']['pageProps'].get('status') == 404:
                # A 200 answer whose page is "Page not found"
                raise UserError(NOT_FOUND[KINDS.get(kind, kind)], 404)
            session = data['props']['pageProps']['state']['settings']['session']
            self.token = session['accessToken']
            self.token_expires = session.get('accessTokenExpirationTimestampMs', 0) / 1000 or time.time() + 600
        except (AttributeError, KeyError, TypeError, json.JSONDecodeError):
            raise SpotifyError('no token in the embed page')
        return data

    async def _discover_hash(self, http, op):
        async with http.get(PLAYER_URL) as res:
            html = await res.text()
        pattern = re.compile(HASH_IN_SCRIPT.format(op=op))
        for url in dict.fromkeys(SCRIPT.findall(html)):
            async with http.get(url) as res:
                m = pattern.search(await res.text())
            if m:
                if m.group(1) in self.hashes[op]:
                    break
                log.info('spotify: using %s hash %s… from the web player', op, m.group(1)[:8])
                self.hashes[op].insert(0, m.group(1))
                return
        raise SpotifyError(f'couldn\'t find a working {op} hash')


NOT_FOUND = {
    'playlist': ('Spotify couldn\'t find that playlist. Check the link, and that the playlist isn\'t '
                 'private: Song-DL can only read public playlists, or ones shared by link.'),
    'album': 'Spotify couldn\'t find that album. Check the link.',
    'song': 'Spotify couldn\'t find that song. Check the link.',
}


def _playlist_v2(body):
    pl = ((body or {}).get('data') or {}).get('playlistV2')
    if not isinstance(pl, dict):
        raise SpotifyError('no playlistV2 in the answer')
    kind = pl.get('__typename')
    if kind != 'Playlist':
        # NotFound, or something like Forbidden for a private playlist
        log.info('spotify: playlist is %s: %s', kind, pl.get('message'))
        raise UserError(NOT_FOUND['playlist'], 404)
    content = pl.get('content') or {}
    if not isinstance(content.get('items'), list) or not isinstance(content.get('totalCount'), int):
        raise SpotifyError('no items in the answer')
    return pl


def _album(body):
    """An album's answer, shaped like a playlist's: content holds its tracksV2."""
    al = ((body or {}).get('data') or {}).get('albumUnion')
    if not isinstance(al, dict):
        raise SpotifyError('no albumUnion in the answer')
    if al.get('__typename') != 'Album':
        log.info('spotify: album is %s: %s', al.get('__typename'), al.get('message'))
        raise UserError(NOT_FOUND['album'], 404)
    tracks = al.get('tracksV2') or al.get('tracks') or {}
    if not isinstance(tracks.get('items'), list) or not isinstance(tracks.get('totalCount'), int):
        raise SpotifyError('no tracks in the answer')
    return {**al, 'content': tracks}


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
