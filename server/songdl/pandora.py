"""Reads Pandora playlist, album and song links. Pandora only answers from the
US, so this only works on a server there.

Pandora's web API checks that a csrftoken cookie and an X-CsrfToken header
match, so a made-up value does. Songs and albums are then described by
catalog/annotateObjects. A playlist needs an anonymous listener's token
(auth/anonymousLogin) for playlists/getTracks, which pages 100 at a time: the
first answer gives the playlist's version, and every later page must ask for
that version. The songs on a page come with details only up to a limit, so
the rest are looked up with annotateObjects too.

Stations aren't lists of songs (Pandora picks as it plays), so they can't be read.
"""

import asyncio
import logging
import re
import secrets
import time

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

API = 'https://www.pandora.com/api/'
PAGE = 100            # songs per getTracks page; Pandora refuses more details than about 100
BATCH = 100           # ids per annotateObjects
MAX_ITEMS = 10_000
TIMEOUT = 30
RETRIES = 3
TOKEN_LIFE = 3600     # Pandora says 4 hours of inactivity; an hour is plenty

PLAYLIST = re.compile(r'\bpandora\.com/playlist/(PL:\d+:\d+)', re.I)
# pandora.com/artist/<artist>/<album>/<song>/TR<id> and pandora.com/artist/<artist>/<album>/AL<id>;
# the id is a share id (TRrrqx5vtdPqc7X) or TR:<number>
SONG = re.compile(r'\bpandora\.com/artist/[^/?#\s]+/[^/?#\s]+/[^/?#\s]+/(TR(?::\d+|[A-Za-z0-9]{10,}))(?=[/?#\s]|$)', re.I)
ALBUM = re.compile(r'\bpandora\.com/artist/[^/?#\s]+/[^/?#\s]+/(AL(?::\d+|[A-Za-z0-9]{10,}))(?=[/?#\s]|$)', re.I)
UNSUPPORTED = [(re.compile(r'\bpandora\.com/station/', re.I),
                'That\'s a Pandora station, which Pandora makes up as it plays, so it has no list of songs '
                'to read. Paste a playlist, album or song link instead.')]

NOT_FOUND = {
    'playlist': 'Pandora couldn\'t find that playlist. Check the link, and that the playlist is public.',
    'album': 'Pandora couldn\'t find that album. Check the link.',
    'song': 'Pandora couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, id) for a Pandora playlist, album or song link, or None."""
    text = str(text or '')
    for kind, pattern in (('playlist', PLAYLIST), ('song', SONG), ('album', ALBUM)):
        if m := pattern.search(text):
            return kind, m.group(1)
    return None


def _row(t):
    return row(t.get('name'), [t.get('artistName') or ''], t.get('albumName'),
               (t.get('duration') or 0) * 1000, t.get('isrc'))


class Pandora:
    name = 'Pandora'

    def __init__(self):
        self.csrf = secrets.token_hex(8)
        self.token = None
        self.token_expires = 0.0
        self.lock = asyncio.Lock()

    async def read(self, kind, xid):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as http:
            if kind == 'song':
                return await self._song(http, xid)
            if kind == 'album':
                return await self._album(http, xid)
            return await self._playlist(http, xid)

    async def _post(self, http, path, body, kind, auth=False):
        headers = {'Cookie': f'csrftoken={self.csrf}', 'X-CsrfToken': self.csrf, 'User-Agent': USER_AGENT,
                   'Content-Type': 'application/json', 'Accept': 'application/json'}
        for attempt in range(RETRIES):
            if auth:
                headers['X-AuthToken'] = await self._token(http)
            async with http.post(API + path, json=body, headers=headers) as res:
                try:
                    answer = await res.json(content_type=None)
                except ValueError:
                    answer = None
                status = res.status
            error = answer.get('errorType') if isinstance(answer, dict) else None
            if error in ('PLAYLIST_NOT_FOUND', 'PLAYLIST_IS_PRIVATE', 'NOT_FOUND'):
                raise UserError(NOT_FOUND[kind], 404)
            if status == 200 and isinstance(answer, dict) and not error:
                return answer
            if auth and status in (400, 401, 403) and attempt == 0:
                self.token = None       # the listener's token ran out: sign in again
                continue
            if status == 429 or status >= 500:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise ServiceError(f'{path} answered {status}: {str(answer)[:200]}')
        raise ServiceError(f'{path}: gave up after {RETRIES} tries')

    async def _token(self, http):
        async with self.lock:
            if not self.token or time.time() > self.token_expires:
                answer = await self._post(http, 'v1/auth/anonymousLogin', {}, 'playlist')
                if not answer.get('authToken'):
                    raise ServiceError('no authToken from anonymousLogin')
                self.token, self.token_expires = answer['authToken'], time.time() + TOKEN_LIFE
            return self.token

    async def _annotate(self, http, ids, kind):
        found = {}
        for i in range(0, len(ids), BATCH):
            found.update(await self._post(http, 'v4/catalog/annotateObjects', {'pandoraIds': ids[i:i + BATCH]}, kind))
        return found

    async def _song(self, http, xid):
        found = await self._annotate(http, [xid], 'song')
        t = next((v for v in found.values() if isinstance(v, dict) and v.get('type') == 'TR'), None)
        if not t:
            raise UserError(NOT_FOUND['song'], 404)
        return Song(t.get('name') or '', t.get('artistName') or '', t.get('albumName') or '', t.get('duration') or None)

    async def _album(self, http, xid):
        found = await self._annotate(http, [xid], 'album')
        al = next((v for v in found.values() if isinstance(v, dict) and v.get('type') == 'AL'), None)
        if not al:
            raise UserError(NOT_FOUND['album'], 404)
        ids = [i for i in al.get('tracks') or [] if isinstance(i, str)][:MAX_ITEMS]
        songs = await self._annotate(http, ids, 'album')
        rows = [_row(songs[i]) for i in ids if songs.get(i, {}).get('name')]
        result = Playlist(xid, al.get('name') or 'Pandora album', al.get('artistName') or '', rows)
        result.unavailable = max(0, len(ids) - len(rows))
        log.info('pandora album %s: %r, %d songs', xid, result.name, len(rows))
        return result

    async def _playlist(self, http, xid):
        first, ids, offset, total, version = None, [], 0, None, 0
        while len(ids) < MAX_ITEMS and (total is None or offset < total):
            answer = await self._post(http, 'v7/playlists/getTracks', {'request': {
                'pandoraId': xid, 'playlistVersion': version, 'offset': offset, 'limit': PAGE,
                'annotationLimit': PAGE, 'allowedTypes': ['TR']}}, 'playlist', auth=True)
            if first is None:
                if answer.get('isPrivate'):
                    raise UserError(NOT_FOUND['playlist'], 404)
                first, total, version = answer, answer.get('totalTracks'), answer.get('version', 0)
                if not isinstance(total, int):
                    raise ServiceError(f'playlist {xid}: no totalTracks')
            page = answer.get('tracks')
            if not isinstance(page, list):
                raise ServiceError(f'playlist {xid}: no tracks at {offset}')
            if not page:
                break
            ids += [t.get('trackPandoraId') for t in page if isinstance(t, dict)]
            offset += len(page)
            known = answer.get('annotations')
            if isinstance(known, dict):
                first.setdefault('_known', {}).update(known)
        if len(ids) < min(total, MAX_ITEMS):
            raise ServiceError(f'playlist {xid}: got {len(ids)} of {total} songs')
        known = first.get('_known', {})
        missing = [i for i in dict.fromkeys(ids) if i and i not in known]
        known.update(await self._annotate(http, missing, 'playlist'))
        owner = next((v.get('displayname') or v.get('fullname') or v.get('webname') or ''
                      for v in known.values() if isinstance(v, dict) and v.get('type') == 'LI'), '')
        rows = [_row(known[i]) for i in ids[:MAX_ITEMS] if (known.get(i) or {}).get('name')]
        result = Playlist(xid, first.get('name') or 'Pandora playlist', owner, rows)
        result.unavailable = max(0, len(ids[:MAX_ITEMS]) - len(rows))
        log.info('pandora playlist %s: %r, %d songs (%d unavailable)', xid, result.name, len(rows), result.unavailable)
        return result
