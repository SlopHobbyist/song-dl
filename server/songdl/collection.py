"""What the Library page shows: the playlists admins saved on the server, and their songs.

Every folder at the top of the server folder is a playlist (ServerQueue.folder_for names
them after the playlist). The downloads folder, where visitors' files go, is left out when
it sits in there too (music/downloads), and so are slskd's UUID-named folders when the two
are the same folder.

A song's title and artist are the ones library.py wrote down when it was saved (what it was
found as), else its tags, else its file name. Its format, bitrate and so on are read from
the file itself, so a DJ copy shows what it really is.

Every file in a playlist's folder is named and tagged after the playlist's song, whatever
was downloaded for it, so the folder can't show a wrong song. The page can: each song comes
with the name of the file it was downloaded as (library.source_of), and a note when that
name or its length says it may not be the playlist's song (`check`). "Wrong song" (wrong)
deletes such a song and makes sure that copy is never taken for it again.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from . import matching, meta
from .engine import clock
from .errors import UserError
from .library import AUDIO, STAGING, YOUTUBE_ID
from .playlist import Track

log = logging.getLogger('songdl')


class Collection:
    def __init__(self, cfg, library):
        self.cfg = cfg
        self.library = library
        self.cache = {}   # path -> ((mtime_ns, size), what was read from the file)
        # Set by the site: folder name -> {(title, artist): the playlist's Track}, from the imports
        # still around, which know the songs' lengths and everyone credited
        self.wanted = lambda folder: {}

    @property
    def root(self) -> Path:
        return self.cfg.server_folder

    def _is_reserved(self, path):
        """The visitors' downloads folder, or one of Song-DL's own (data, slskd, certs) when
        they happen to sit in the server folder."""
        cfg = self.cfg
        for other in (cfg.downloads_dir, cfg.data_dir, cfg.slskd_dir, cfg.certs_dir):
            if path == other:
                return True
            try:
                if os.path.samefile(path, other):
                    return True
            except OSError:
                pass
        return False

    def _folders(self):
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return []
        out = []
        for entry in entries:
            if entry.name.startswith('.') or STAGING.match(entry.name.lower()):
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            path = self.root / entry.name
            if not self._is_reserved(path):
                out.append(path)
        return out

    def _folder(self, name):
        name = str(name or '')
        for path in self._folders():
            if path.name == name:
                return path
        raise UserError('That playlist isn\'t on the server any more.', 404)

    @staticmethod
    def _files(folder):
        """(path, its place in the folder) of every song in it, sub-folders too."""
        for dirpath, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for name in files:
                if not name.startswith('.') and os.path.splitext(name)[1].lower() in AUDIO:
                    path = Path(dirpath) / name
                    yield path, path.relative_to(folder).as_posix()

    def _song(self, name, rel):
        """The song `rel` in the playlist `name`, as library.py keys it, checked to be in there."""
        folder = self._folder(name)
        rel = str(rel or '')
        path = folder / rel
        try:
            real = path.resolve()
            real.relative_to(folder.resolve())
        except (OSError, ValueError):
            raise UserError('That song isn\'t on the server any more.', 404)
        if not rel or real.suffix.lower() not in AUDIO or not real.is_file():
            raise UserError('That song isn\'t on the server any more.', 404)
        return path

    # What the page asks for

    async def playlists(self):
        return await asyncio.to_thread(self._playlists)

    def _playlists(self):
        out = []
        for folder in self._folders():
            count = size = 0
            newest = 0
            for path, _ in self._files(folder):
                try:
                    st = path.stat()
                except OSError:
                    continue
                count += 1
                size += st.st_size
                newest = max(newest, st.st_mtime)
            out.append({'name': folder.name, 'songs': count, 'size': size, 'modified': newest or None})
        out.sort(key=lambda p: p['name'].casefold())
        return out

    async def songs(self, name):
        return await asyncio.to_thread(self._songs, name)

    def _songs(self, name):
        folder = self._folder(name)
        out, seen = [], set()
        wanted = self.wanted(folder.name)
        for path, rel in self._files(folder):
            song = self._describe(path, rel, wanted)
            if song:
                out.append(song)
                seen.add(str(path))
        for key in [k for k in self.cache if k.startswith(str(folder) + os.sep) and k not in seen]:
            del self.cache[key]
        out.sort(key=lambda s: (s['artist'].casefold(), s['title'].casefold(), s['path']))
        return out

    def file(self, name, rel):
        return self._song(name, rel)

    async def zip_files(self, name):
        return await asyncio.to_thread(self._zip_files, name)

    def _zip_files(self, name):
        """[(its place in the playlist, path)] of every song in it, by name, for a zip. A song
        that is really somewhere outside the playlist (a symlink) is left out."""
        folder = self._folder(name)
        inside = folder.resolve()
        out = []
        for path, rel in self._files(folder):
            try:
                path.resolve().relative_to(inside)
            except (OSError, ValueError):
                continue
            out.append((rel, path))
        out.sort(key=lambda f: f[0].casefold())
        return out

    async def delete(self, name, rel):
        path = self._song(name, rel)
        folder = self._folder(name)
        await asyncio.to_thread(_remove, path, folder)
        self.library._drop(str(path))
        self.cache.pop(str(path), None)
        log.info('deleted %s from the library', path)

    async def wrong(self, name, rel):
        """An admin says the song isn't what its name says. Deletes it, with the download it
        was made from (so it's neither handed out again nor shared under that name), and
        blocks that copy for the song. Returns the song as _describe gives it, to look for again."""
        path = self._song(name, rel)
        folder = self._folder(name)
        song = await asyncio.to_thread(self._describe, path, rel, self.wanted(folder.name))
        if not song:
            raise UserError('That song isn\'t on the server any more.', 404)
        rec = self.library.songs.get(str(path))
        self.library.block(song['title'], song['artist'], self.library.source_of(rec))
        behind = await asyncio.to_thread(self.library.copies_behind, path)
        await asyncio.to_thread(_remove, path, folder)
        for other in behind:
            await asyncio.to_thread(_remove, Path(other), Path(other).parent)
            self.library._drop(other)
        self.library._drop(str(path))
        self.cache.pop(str(path), None)
        log.info('%s was the wrong song: deleted, with %d file(s) it was made from', path, len(behind))
        return song

    async def delete_playlist(self, name):
        """Deletes the playlist's folder and everything in it. Its "(DJ)" copies are a
        playlist of their own and stay."""
        folder = self._folder(name)
        if folder.is_symlink():
            raise UserError('That playlist is a link to another folder, so it can\'t be deleted here.', 409)
        try:
            await asyncio.to_thread(shutil.rmtree, folder)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise UserError(f'Couldn\'t delete all of that playlist: {e.strerror or e}.', 500)
        finally:
            # Whatever went, even when something in there couldn't be deleted
            inside = str(folder) + os.sep
            for key in [k for k in self.library.songs if k.startswith(inside) and not os.path.exists(k)]:
                self.library._drop(key)
            for key in [k for k in self.cache if k.startswith(inside)]:
                del self.cache[key]
        log.info('deleted the playlist %s', folder)

    def _check(self, title, artist, info, rec, src, track):
        """Why a song may not be what its name says, or None: the file it was downloaded as
        doesn't pass the checks a new download would (matching.fit), or it's another length."""
        said = src.get('length') if src else None
        # How long it really plays: measured when it was downloaded, else what the file here says
        played = (src.get('measured') if src else None) or (rec.get('measured') if rec else None) or info['length']
        if said and played and played < said - matching.LENGTH_TOLERANCE:
            return f'Only {clock(played)} of it plays; the file it came from said {clock(said)}'
        played = played or said
        duration = track.duration if track else None
        if duration and played:
            off = played - duration
            how = matching.length_fit(played, duration, matching.wants_exact(track))
            if off < -matching.LENGTH_TOLERANCE:
                return f'{clock(-off)} shorter than the playlist\'s version'
            if how is None:
                return f'{clock(off)} longer than the playlist\'s version'
        if src and src.get('name'):
            track = track or Track(title, artist)
            original = src['name'].replace('\\', '/').rsplit('/', 1)[-1]
            if src.get('origin') in matching.ELSEWHERE:
                said_words = set(matching.words(original)) | set(matching.words(src.get('from') or ''))
                artists = matching.credited(track)
                named = set(matching.key_words(src.get('from') or '')) <= set(matching.words(track.title))   # its remixer
                if artists and not matching.artist_share(artists, said_words) and src.get('from') and not named and \
                        not matching.TOPIC_CHANNEL.search(src['from']):
                    return f'Uploaded by {src["from"]}, who isn\'t credited with the song'
            elif not matching.names_song(src['name'], track):
                return 'Downloaded as a file with another name'
            elif matching._is_variant(src['name'], matching.asked_words(track), longer=True):
                return 'The file it came from looks like another version'
        if duration and played and played - duration > matching.LENGTH_TOLERANCE:
            return f'A longer version: {clock(played)}, and the playlist\'s is {clock(duration)}'
        return None

    def _describe(self, path: Path, rel, wanted=None):
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        stamp = (st.st_mtime_ns, st.st_size)
        cached = self.cache.get(key)
        if cached and cached[0] == stamp:
            info = cached[1]
        else:
            info = _read(path)
            self.cache[key] = (stamp, info)
        rec = self.library.songs.get(key) or {}
        title, artist = rec.get('title') or info['title'], rec.get('artist') or info['artist']
        if not title:
            guess = meta.parse(YOUTUBE_ID.sub('', path.name))
            title, artist = guess['title'] or path.stem, artist or guess['artist']
        fmt = AUDIO[path.suffix.lower()]
        src = self.library.source_of(rec) or {}
        track = (wanted or {}).get((rec.get('title'), rec.get('artist'))) if rec else None
        return {
            # What it was downloaded as, and from where
            'original': src.get('name', '').replace('\\', '/').rsplit('/', 1)[-1] or None,
            'source': src.get('origin'), 'from': src.get('from'), 'link': src.get('link'),
            'check': self._check(title, artist or '', info, rec, src, track),
            'album': rec.get('album') or '',
            'path': rel, 'title': title, 'artist': artist or '', 'format': fmt.upper(),
            'bitrate': info['bitrate'] or rec.get('bitrate'),
            'bitDepth': info['bit_depth'] or rec.get('bit_depth'),
            'sampleRate': info['sample_rate'] or rec.get('sample_rate'),
            'length': info['length'] or rec.get('length'), 'size': st.st_size,
            'dj': bool(rec.get('dj')),
        }


def _read(path):
    """Tags and stream details from the file; whatever can't be read is None."""
    out = dict.fromkeys(('title', 'artist', 'bitrate', 'bit_depth', 'sample_rate', 'length'))
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
    except ImportError:
        return out
    except Exception as e:   # a broken or unusual file
        log.debug('can\'t read %s: %r', path, e)
        return out
    if f is None:
        return out
    tags = f.tags or {}

    def tag(key):
        try:
            v = tags.get(key)
        except (KeyError, ValueError):
            return None
        return (v[0] if isinstance(v, list) and v else v) or None

    info = f.info
    out['title'], out['artist'] = tag('title'), tag('artist')
    if getattr(info, 'bitrate', None):
        out['bitrate'] = round(info.bitrate / 1000)
    out['bit_depth'] = getattr(info, 'bits_per_sample', None) or None
    out['sample_rate'] = getattr(info, 'sample_rate', None) or None
    out['length'] = round(info.length) if getattr(info, 'length', None) else None
    return out


def _remove(path: Path, folder: Path):
    """Deletes the song, and any sub-folder of the playlist that leaves empty."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise UserError(f'Couldn\'t delete that song: {e.strerror or e}.', 500)
    parent = path.parent
    while parent != folder and folder in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
