"""What the Library page shows: the playlists admins saved on the server, and their songs.

Every folder at the top of the server folder is a playlist (ServerQueue.folder_for names
them after the playlist). The downloads folder, where visitors' files go, is left out when
it sits in there too (music/downloads), and so are slskd's UUID-named folders when the two
are the same folder.

A song's title and artist are the ones library.py wrote down when it was saved (what it was
found as), else its tags, else its file name. Its format, bitrate and so on are read from
the file itself, so a DJ copy shows what it really is.
"""

import asyncio
import logging
import os
import shutil
from pathlib import Path

from . import meta
from .errors import UserError
from .library import AUDIO, STAGING, YOUTUBE_ID

log = logging.getLogger('songdl')


class Collection:
    def __init__(self, cfg, library):
        self.cfg = cfg
        self.library = library
        self.cache = {}   # path -> ((mtime_ns, size), what was read from the file)

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
        for path, rel in self._files(folder):
            song = self._describe(path, rel)
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

    def _describe(self, path: Path, rel):
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
        return {
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
