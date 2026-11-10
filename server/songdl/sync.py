"""Sync playlist: brings a playlist saved on the server up to date with the playlist on
Spotify, YouTube or any other service links.py reads, the one it was made from.

When an admin imports a playlist link, or saves a scanned one to the server, the link is
written down here (data/playlist-links.json) against the playlist's folder, together with
every song the playlist had then. Syncing reads the link again and imports only the songs
that weren't on it before, with the formats, sources and DJ mode it was saved with. Songs
taken off the playlist there are left alone here and aren't counted: a sync only adds.

No song is fetched twice:
- the songs a playlist had are written down for good, so one that was there before is never
  new again, even when it was deleted here or taken off the playlist and put back;
- a new song that is already in the folder (saved by hand, or spelled a little differently)
  is skipped;
- a song that is on the playlist twice is taken once;
- a playlist is synced once at a time, and the next sync sees what the last one took;
- and the import looks among the files already on the server before it searches, and the
  queue won't save a song the folder already has (imports.py, server_queue.py).
"""

import json
import logging
import os
import time

from . import imports, library, links, matching, playlist
from .errors import UserError
from .playlist import Track
from .server_queue import normalize_folder

log = logging.getLogger('songdl')

NOT_LINKED = 'This playlist wasn\'t added from a link, so there\'s nothing to sync it with.'


def _title_words(title):
    title = playlist.clean_title(title or '')
    return library.core_words(title) or library.words(title)


song_key = library.song_key   # which song a playlist entry is, however the service spells it this time


def _same_song(track: Track, song):
    """Whether a song in the folder (as Collection describes it) is this playlist entry."""
    if _title_words(track.title) != _title_words(song['title']):
        return False
    a, b = library.words(track.artist), library.words(song['artist'])
    return not a or not b or bool(a & b)


class Links:
    """The link each playlist folder came from, and every song it has had on it."""

    def __init__(self, cfg):
        self.file = cfg.links_file
        self.links = {}   # folder name -> record

    def load(self):
        try:
            data = json.loads(self.file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning('can\'t read %s: %s', self.file, e)
            return
        self.links = {k: v for k, v in (data.get('playlists') or {}).items() if isinstance(v, dict) and v.get('url')}

    def save(self):
        tmp = self.file.with_name(self.file.name + '.tmp')
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({'version': 1, 'playlists': self.links}), encoding='utf-8')
            os.replace(tmp, self.file)
        except OSError as e:
            log.warning('can\'t save %s: %s', self.file, e)

    def get(self, folder):
        return self.links.get(folder)

    async def remember(self, folder, url, name, tracks, formats, sources, dj):
        """Writes down that the playlist in `folder` came from `url` and had these songs.
        Saving the same link into the folder again adds to its songs."""
        url = str(url or '').strip()[:2000]
        try:
            link = links.check(url) or await links.resolve(url)
        except UserError as e:
            log.info('not remembering %r for %r: %s', url, folder, e)
            return
        if link.kind == 'song':
            return
        rec = self.links.get(folder) or {}
        same = (rec.get('service'), rec.get('kind'), rec.get('id')) == (link.service, link.kind, link.id)
        known = set(rec.get('known') or []) if same else set()
        known |= {song_key(t.title, t.artist) for t in tracks}
        self.links[folder] = {
            'url': url, 'service': link.service, 'sourceName': link.name, 'kind': link.kind, 'id': link.id,
            'name': ' '.join(str(name or '').split())[:120] or folder,
            'formats': list(formats), 'sources': list(sources), 'dj': bool(dj),
            'known': sorted(known),
            'linked': rec.get('linked') if same else int(time.time() * 1000),
            'synced': rec.get('synced') if same else None,
            'added': rec.get('added') if same else None,
        }
        self.save()
        log.info('playlist %r is linked to %s %s %s (%d songs)', folder, link.service, link.kind, link.id, len(known))

    def forget(self, folder):
        """The playlist was deleted: a playlist saved under its name later starts afresh."""
        if self.links.pop(folder, None) is not None:
            self.save()

    def view(self, folder):
        rec = self.links.get(folder)
        if not rec:
            return None
        return {'url': rec['url'], 'source': rec.get('service'), 'sourceName': rec.get('sourceName') or 'the link',
                'kind': rec.get('kind') or 'playlist', 'synced': rec.get('synced'), 'added': rec.get('added')}


class Syncer:
    def __init__(self, links_: Links, collection, importer: imports.Importer, readers: links.Readers):
        self.links = links_
        self.collection = collection
        self.importer = importer
        self.readers = readers
        self.busy = set()   # folders being synced

    async def sync(self, name, by):
        folder = self.collection._folder(name)   # 404 when it isn't a playlist on the server
        rec = self.links.get(folder.name)
        if not rec:
            raise UserError(NOT_LINKED, 409)
        if folder.name in self.busy:
            raise UserError('That playlist is being synced already.', 409)
        self.busy.add(folder.name)
        try:
            return await self._sync(folder, rec, by)
        finally:
            self.busy.discard(folder.name)

    async def _sync(self, folder, rec, by):
        reader = self.readers.readers.get(rec.get('service'))
        if not reader:
            raise UserError(NOT_LINKED, 409)
        if normalize_folder(folder.name) != folder.name:
            raise UserError('That playlist\'s folder name can\'t be synced into. Rename the folder first.', 409)
        link = links.Link(rec['service'], reader.name, rec['kind'], rec['id'])
        found = await self.readers.read(link)
        if isinstance(found, links.Song):
            raise UserError(f'That link is a song now, not a {link.kind}.', 502)
        tracks = []
        if any(r.get('Track Name') for r in found.rows):
            tracks, _ = playlist.parse(found.csv().encode('utf-8'), limit=10 ** 9)
            matching.mark_versions(tracks)   # over the whole playlist, before the new songs are picked out

        known = set(rec.get('known') or [])
        added, seen = [], set()
        for t in tracks:
            key = song_key(t.title, t.artist)
            if key not in known and key not in seen:
                seen.add(key)
                added.append(t)
        here = await self.collection.songs(folder.name) if added else []
        fresh = [t for t in added if not any(_same_song(t, s) for s in here)]

        imp = None
        if fresh:
            sources = [s for s in rec.get('sources') or () if s in matching.SOURCES] or ['soulseek']
            formats = [f for f in rec.get('formats') or () if f in matching.FORMATS]
            if 'soulseek' in sources and not formats:
                formats = ['wav', 'flac']
            imp = self.importer.add(f'{rec.get("name") or folder.name} (sync)', formats, folder.name, by, fresh,
                                    0, sources, rec.get('dj'))
        # Only once the import is in: a sync that failed leaves the new songs new
        rec['known'] = sorted(known | {song_key(t.title, t.artist) for t in tracks})
        rec['synced'] = int(time.time() * 1000)
        rec['added'] = len(added)
        self.links.save()
        log.info('%s synced %r with %s: %d songs there, %d new, %d already here', by, folder.name, link.service,
                 len(tracks), len(added), len(added) - len(fresh))
        return {'total': len(tracks), 'added': len(added), 'already': len(added) - len(fresh),
                'queued': len(fresh), 'import': imp.view(self.importer.queue) if imp else None, 'link': self.links.view(folder.name)}
