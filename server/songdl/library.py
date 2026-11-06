"""Songs already on this server, so a song someone fetched before is handed out
again at once instead of being searched for and downloaded a second time.

Everything Song-DL downloads lands under the downloads folder: visitors' files
in the UUID-named folders slskd saves them in (deleted once nobody has used
them for downloads.max_file_age days), admins' songs in the folders they pick.
Those file names often don't say what the song is ("03 - Digital Love.flac"),
so each download is written down here with the title and artist it was found
for. Files that got there some other way are read from their tags, or failing
that from their names.

Searches, scans and imports look here before any source the visitor picked
(see jobs.py), always, and take a copy that is in one of the visitor's formats
or came from one of their sources; the earliest of those in the visitor's
order wins. A copy handed out again is hard-linked into a new download folder,
which takes no extra space, and its clock for deletion starts over.
"""

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path

from . import matching, meta

log = logging.getLogger('songdl')

LENGTH_TOLERANCE = 8      # seconds a copy may differ from the length a playlist gives
REFRESH_SECONDS = 600     # how often the folders are looked through for files that came or went
SAVE_SECONDS = 30
USERNAME = 'Song-DL'      # what a local copy's Candidate says it's from

AUDIO = {ext: fmt for fmt, exts in matching.EXTENSIONS.items() for ext in exts}
# A bracketed part of a title that doesn't change which song it is: a featured artist, a
# format, or an id some site added ("[0mdYVvWJemI]")
NOISE = re.compile(r'\s*[(\[{](?:(?:feat|ft|featuring|with)\b[^)\]}]*'
                   r'|[^)\]}\s]*\d[^)\]}\s]*[a-z][^)\]}\s]*|[^)\]}\s]*[a-z][^)\]}\s]*\d[^)\]}\s]*)[)\]}]', re.I)
FEAT_TAIL = re.compile(r'\s+(?:feat|ft|featuring)\b.*$', re.I)
YOUTUBE_ID = re.compile(r'\[[A-Za-z0-9_-]{11}\]')
FORMAT_WORDS = set(matching.FORMATS) | set(AUDIO.values())
# slskd saves a visitor's file in a folder named by a random UUID (as engine.STAGING)
STAGING = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def words(text):
    """Its words, lower case and without accents, so "Río" is "rio"."""
    text = unicodedata.normalize('NFKD', text or '')
    return set(matching.tokens(''.join(ch for ch in text if not unicodedata.combining(ch))))


def core_words(title):
    """The words that say which song a title is: without featured artists or ids."""
    title = FEAT_TAIL.sub('', NOISE.sub(' ', meta.FORMAT_TAG.sub(' ', title or '')))
    return words(title) - FORMAT_WORDS


def steps(formats, sources):
    """The visitor's order as one list: their formats where Soulseek is, then the other sources."""
    out = []
    for source in sources:
        out += list(formats) if source == 'soulseek' else [source]
    return out


class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.file = cfg.data_dir / 'library.json'
        self.songs = {}     # path -> record (a dict, as saved)
        self.index = {}     # word -> paths whose record has it
        self.dirty = False
        self.refreshed = 0

    @property
    def roots(self):
        roots = [self.cfg.downloads_dir]
        if self.cfg.server_folder != self.cfg.downloads_dir:
            roots.append(self.cfg.server_folder)
        return roots

    # Keeping the index

    def _put(self, rec):
        path = rec['path']
        self._drop(path)
        rec['words'] = sorted(words(' '.join(
            str(rec.get(k) or '') for k in ('artist', 'title', 'album', 'name', 'query'))) - FORMAT_WORDS)
        self.songs[path] = rec
        for w in rec['words']:
            self.index.setdefault(w, set()).add(path)
        self.dirty = True

    def _drop(self, path):
        rec = self.songs.pop(path, None)
        if rec:
            for w in rec['words']:
                paths = self.index.get(w)
                if paths:
                    paths.discard(path)
                    if not paths:
                        del self.index[w]
            self.dirty = True

    def add(self, path: Path, title, artist, album, cand: matching.Candidate, query=None):
        """Writes down a finished download, or a song saved into an admin's folder: the song it
        was found as, and where it came from."""
        try:
            size = path.stat().st_size
        except OSError:
            return
        if cand.source == 'local':   # another copy of a song that's here: same song, same origin
            origin, name = cand.origin, Path(cand.filename).name
            who, link = cand.meta.get('from'), cand.meta.get('link')
        else:
            origin, name = cand.source, cand.filename if cand.source == 'soulseek' else cand.basename
            who, link = (cand.username if cand.source != 'soulseek' else None), cand.url
        self._put({
            'path': str(path), 'title': title or '', 'artist': artist or '', 'album': album or '',
            'fmt': cand.fmt, 'size': size, 'length': cand.length, 'bit_depth': cand.bit_depth,
            'sample_rate': cand.sample_rate, 'bitrate': cand.bitrate if cand.fmt in matching.LOSSY else None,
            'origin': origin, 'name': name, 'from': who, 'link': link, 'query': query or '',
            'added': time.time(),
        })

    def load(self):
        try:
            data = json.loads(self.file.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning('can\'t read %s: %s', self.file, e)
            return
        for rec in data.get('songs', []):
            if isinstance(rec, dict) and rec.get('path') and rec.get('fmt') in matching.EXTENSIONS:
                self._put(rec)
        self.dirty = False

    def save(self):
        if not self.dirty:
            return
        self.dirty = False
        songs = [{k: v for k, v in rec.items() if k != 'words'} for rec in self.songs.values()]
        tmp = self.file.with_suffix('.tmp')
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({'songs': songs}, ensure_ascii=False), encoding='utf-8')
            os.replace(tmp, self.file)
        except OSError as e:
            log.warning('can\'t save %s: %s', self.file, e)
            self.dirty = True

    async def refresh(self):
        """Forgets files that are gone, and writes down ones that came some other way."""
        known = set(self.songs)
        found, new = await asyncio.to_thread(_walk, self.roots, known)
        for path in known - found:
            if path in self.songs and not os.path.exists(path):
                self._drop(path)
        for path, same, rec in new:
            if path in self.songs:
                continue
            if same in self.songs:   # another link to a file written down already: the same song
                rec = {**self.songs[same], 'path': path}
                rec.pop('words', None)
            if rec:
                self._put(rec)
        self.refreshed = time.monotonic()
        if new:
            log.info('library: %d new file(s), %d in all', len(new), len(self.songs))

    async def run(self):
        """Keeps the index up to date with the folders, and saved."""
        while True:
            try:
                if time.monotonic() - self.refreshed >= REFRESH_SECONDS:
                    await self.refresh()
                await asyncio.to_thread(self.save)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception('library upkeep failed')
            await asyncio.sleep(SAVE_SECONDS)

    def shutdown(self):
        self.save()

    # Looking things up

    def find(self, query, order, duration=None):
        """The best copy here of the song `query` asks for, as a list of Candidates (best
        first), or []. `order` is steps(): only copies in one of those formats or from one
        of those sources count, and the earliest wins. `duration` is the playlist's length."""
        wanted = words(query) - FORMAT_WORDS
        if not wanted or not order:
            return []
        paths = None
        for w in sorted(wanted, key=lambda w: len(self.index.get(w, ()))):
            paths = set(self.index.get(w, ())) if paths is None else paths & self.index.get(w, set())
            if not paths:
                return []
        hits = []
        for path in paths:
            rec = self.songs[path]
            step = _step(rec)
            if step not in order:
                continue
            # The query names this song, not just its artist: every word of its title is asked for
            core = core_words(rec.get('title')) - words(rec.get('artist'))
            if not core or not core <= wanted:
                continue
            if matching._is_variant(rec.get('name') or '', wanted):
                continue   # a remix or live take the query didn't ask for
            if duration and rec.get('length') and abs(rec['length'] - duration) > LENGTH_TOLERANCE:
                continue
            hits.append((order.index(step), rec))
        if not hits:
            return []
        best = min(i for i, _ in hits)
        cands = [_candidate(rec) for i, rec in hits if i == best]
        cands.sort(key=lambda c: (c.quality, c.meta.get('added') or 0), reverse=True)
        cands = [c for c in cands if os.path.isfile(c.filename)]
        return cands[:3]


def _step(rec):
    """Where a copy stands in the visitor's order: its format if it came from Soulseek, else its source."""
    origin = rec.get('origin')
    if origin in matching.ELSEWHERE:
        return origin
    if origin == 'soulseek' or rec['fmt'] in matching.FORMATS:
        return rec['fmt']
    # Lossy audio from who knows where: what yt-dlp would have made it from
    return 'youtube' if rec['fmt'] != 'm4a' or YOUTUBE_ID.search(rec['path']) else 'soundcloud'


def _candidate(rec):
    step = _step(rec)
    return matching.Candidate(
        username=USERNAME, filename=rec['path'], size=rec['size'], length=rec.get('length'),
        bit_depth=rec.get('bit_depth'), sample_rate=rec.get('sample_rate'), free_slot=True, queue=0,
        speed=0, fmt=rec['fmt'], bitrate=rec.get('bitrate'), source='local',
        origin=rec.get('origin') or (step if step in matching.ELSEWHERE else None),
        meta={'title': rec.get('title'), 'artist': rec.get('artist'), 'album': rec.get('album'),
              'from': rec.get('from'), 'link': rec.get('link'), 'added': rec.get('added')},
    )


def _walk(roots, known):
    """Every audio file under the roots, and for each not in `known`: (path, the known path
    that is the same file (a hard link) or None, a record read from the file or None)."""
    found, new = set(), []
    inodes = {}
    for path in known:
        try:
            st = os.stat(path)
            inodes[(st.st_dev, st.st_ino)] = path
        except OSError:
            pass
    # The downloads folder may sit inside the server folder (music/downloads), mounted at a
    # path of its own too: it's walked once, as its own root
    root_ids = {}
    for root in roots:
        try:
            st = os.stat(root)
            root_ids[(st.st_dev, st.st_ino)] = root
        except OSError:
            pass

    def other_root(folder, name):
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            return False
        return (st.st_dev, st.st_ino) in root_ids

    for root in roots:
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not d.startswith('.') and not other_root(folder, d)]
            for name in files:
                fmt = AUDIO.get(os.path.splitext(name)[1].lower())
                if not fmt or name.startswith('.'):
                    continue
                path = os.path.join(folder, name)
                found.add(path)
                if path in known:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                same = inodes.get((st.st_dev, st.st_ino))
                new.append((path, same, None if same else _read(Path(path), fmt)))
    return found, new


def _read(path: Path, fmt):
    """A record for a file nobody wrote down: from its tags, else its name."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size < 200_000:
        return None
    title = artist = album = None
    length = depth = rate = bitrate = None
    try:
        import mutagen
        f = mutagen.File(path, easy=True)
        if f is not None:
            tags = f.tags or {}

            def tag(key):
                try:
                    v = tags.get(key)
                except (KeyError, ValueError):
                    return None
                return (v[0] if isinstance(v, list) and v else v) or None

            title, artist, album = tag('title'), tag('artist'), tag('album')
            info = f.info
            length = round(info.length) if getattr(info, 'length', None) else None
            depth = getattr(info, 'bits_per_sample', None)
            rate = getattr(info, 'sample_rate', None)
            if fmt in matching.LOSSY and getattr(info, 'bitrate', None):
                bitrate = round(info.bitrate / 1000)
    except ImportError:
        pass
    except Exception as e:   # a broken or unusual file: go by its name
        log.debug('can\'t read tags of %s: %r', path, e)
    if not title:
        # A visitor's file sits in a folder named by a random id, which says nothing
        name = path.name if STAGING.match(path.parent.name) else f'{path.parent.name}/{path.name}'
        guess = meta.parse(YOUTUBE_ID.sub('', name))
        title, artist, album = guess['title'], artist or guess['artist'], album or guess['album']
    return {
        'path': str(path), 'title': title or path.stem, 'artist': artist or '', 'album': album or '',
        'fmt': fmt, 'size': size, 'length': length, 'bit_depth': depth, 'sample_rate': rate,
        'bitrate': bitrate, 'origin': None, 'name': path.name, 'from': None, 'link': None, 'query': '',
        'added': path.stat().st_mtime,
    }

