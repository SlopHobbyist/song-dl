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

DJ mode's copies (dj.py) are written down too, marked `dj`, so the same file is
never converted twice, but they are never handed out as originals: a WAV
decoded from YouTube's Opus is no WAV from Soulseek.

A file is written down under the song it was downloaded *for*, which is not
proof that it is that song. So a copy is only handed out for a playlist's song
when the name it was downloaded under passes the same checks a new download
would (matching.fit), and a copy an admin has called the wrong song (block) is
never taken for that song again, here or from where it came.
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
from .playlist import clean_title

log = logging.getLogger('songdl')

LENGTH_TOLERANCE = matching.LENGTH_TOLERANCE   # seconds a copy may differ from the length a playlist gives
SOURCE_GAP = 900          # seconds within which a DJ copy was made from the original written down before it
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


def song_key(title, artist):
    """Which song a playlist entry is, however the service spells it this time:
    "Daft Punk - Digital Love (feat. X) - Remastered" and "daft punk - Digital Love" are one."""
    title = clean_title(title or '')
    return ' '.join(sorted(words(artist))) + '|' + ' '.join(sorted(core_words(title) or words(title)))


def copy_id(name=None, link=None):
    """Which copy of a song a file is, wherever it's kept and whoever shares it: its link on
    SoundCloud or YouTube, else its file name."""
    return link or 'file:' + (name or '').replace('\\', '/').rsplit('/', 1)[-1].lower()


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
        self.dj = {}        # dj.source_key of an original -> path of its CDJ copy
        self.by_song = {}   # (title, artist) as written down -> paths
        self.blocked = {}   # song_key -> the copy_ids an admin called the wrong song
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
        # Only a copy in a download folder can be served again (a link to it in an admin's folder can't)
        if isinstance(rec.get('dj'), str) and STAGING.match(Path(path).parent.name):
            self.dj[rec['dj']] = path
        for w in rec['words']:
            self.index.setdefault(w, set()).add(path)
        self.by_song.setdefault((rec.get('title') or '', rec.get('artist') or ''), set()).add(path)
        self.dirty = True

    def _drop(self, path):
        rec = self.songs.pop(path, None)
        if rec:
            if self.dj.get(rec.get('dj')) == path:
                del self.dj[rec['dj']]
            song = (rec.get('title') or '', rec.get('artist') or '')
            self.by_song.get(song, set()).discard(path)
            if not self.by_song.get(song):
                self.by_song.pop(song, None)
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
            origin, name = cand.origin, cand.meta.get('name') or Path(cand.filename).name
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
            # How long it really plays, and how that suits the playlist it was saved for
            'measured': cand.measured, 'fit': cand.fit or None,
        })

    def add_dj(self, path: Path, title, artist, album, key, label, made_from=None):
        """Writes down a CDJ copy made by dj.py from the file `key` (dj.source_key) names. Only
        ever looked up by that key (dj_copy), never by find(). `made_from` is the (path,
        Candidate) of the download it was made from, so the Library can say what a song in a
        DJ folder really is."""
        try:
            size = path.stat().st_size
        except OSError:
            return
        fmt = AUDIO.get(path.suffix.lower(), 'wav')
        rec = {
            'path': str(path), 'title': title or '', 'artist': artist or '', 'album': album or '',
            'fmt': fmt, 'size': size, 'length': None, 'bit_depth': None, 'sample_rate': None, 'bitrate': None,
            'origin': None, 'name': path.name, 'from': None, 'link': None, 'query': '', 'added': time.time(),
            'dj': key, 'dj_label': label,
        }
        if made_from:
            src, cand = made_from
            original = self.songs.get(str(src)) or {}
            rec['src'] = {'path': str(src), 'origin': original.get('origin'), 'name': original.get('name'),
                          'from': original.get('from'), 'link': original.get('link'), 'length': cand.length,
                          'measured': cand.measured, 'fit': cand.fit or None}
        self._put(rec)

    # What a saved song really is, and the copies that turned out not to be it

    def source_of(self, rec):
        """What a saved song was downloaded as: the original file's record (or what was kept
        of it), with its name, where it came from and its length. None when nobody knows."""
        if not rec:
            return None
        if not rec.get('dj'):
            return rec if rec.get('origin') else None
        if rec.get('src'):
            return rec['src']
        # A DJ copy from before those were written down: the original saved just before it
        when = rec.get('added') or 0
        song = (rec.get('title') or '', rec.get('artist') or '')
        before = [r for r in (self.songs.get(p) for p in self.by_song.get(song, ()))
                  if r and not r.get('dj') and r.get('origin') and (r.get('added') or 0) <= when + 5]
        best = max(before, key=lambda r: r.get('added') or 0, default=None)
        return best if best and when - (best.get('added') or 0) < SOURCE_GAP else None

    def copies_behind(self, path):
        """The files in the download folders a saved song was made from: its original, and
        the CDJ copy it is a link to. Other playlists' copies aren't among them."""
        rec = self.songs.get(str(path))
        if not rec:
            return []
        src = self.source_of(rec) or {}
        out = []
        for p in self.by_song.get((rec.get('title') or '', rec.get('artist') or ''), ()):
            other = self.songs.get(p)
            if not other or p == str(path) or not STAGING.match(Path(p).parent.name):
                continue
            if other.get('dj'):
                try:
                    same = os.path.samefile(p, path)
                except OSError:
                    same = False
                if same:
                    out.append(p)
            elif p == src.get('path') or (src.get('name') and other.get('name') == src.get('name')
                                          and other.get('link') == src.get('link')):
                out.append(p)
        return out

    def block(self, title, artist, src):
        """Never take the copy `src` (see source_of) for this song again."""
        if not src or not (src.get('name') or src.get('link')):
            return
        copies = self.blocked.setdefault(song_key(title, artist), [])
        cid = copy_id(src.get('name'), src.get('link'))
        if cid not in copies:
            copies.append(cid)
            self.dirty = True
            log.info('library: %r is not %s - %s', cid, artist, title)

    def is_blocked(self, title, artist, cand: matching.Candidate):
        """Whether an admin has said this copy isn't that song."""
        copies = self.blocked.get(song_key(title, artist)) if self.blocked else None
        if not copies:
            return False
        if cand.source == 'local':
            return copy_id(cand.meta.get('name') or cand.filename, cand.meta.get('link')) in copies
        return copy_id(cand.filename, cand.url) in copies

    def dj_copy(self, key):
        """(path, label) of the CDJ copy made from the file `key` names, if it's still here and in
        one of the download folders (which are served), else None."""
        path = self.dj.get(key)
        if not path or not os.path.isfile(path):
            return None
        return Path(path), self.songs[path].get('dj_label') or ''

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
        blocked = data.get('blocked')
        self.blocked = {k: list(v) for k, v in blocked.items() if isinstance(v, list)} if isinstance(blocked, dict) else {}
        self.dirty = False

    def save(self):
        if not self.dirty:
            return
        self.dirty = False
        songs = [{k: v for k, v in rec.items() if k != 'words'} for rec in self.songs.values()]
        tmp = self.file.with_suffix('.tmp')
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({'songs': songs, 'blocked': self.blocked}, ensure_ascii=False),
                           encoding='utf-8')
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

    def find(self, query, order, duration=None, track=None):
        """The best copy here of the song `query` asks for, as a list of Candidates (best
        first), or []. `order` is steps(): only copies in one of those formats or from one
        of those sources count, and the earliest wins. `duration` is the playlist's length;
        `track` the playlist's song, which the copy then has to be by its own name too."""
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
            if rec.get('dj'):
                continue   # made for DJ mode, not as it came
            step = _step(rec)
            if step not in order:
                continue
            # The query names this song, not just its artist: every word of its title is asked for
            core = core_words(rec.get('title')) - words(rec.get('artist'))
            if not core or not core <= wanted:
                continue
            if matching._is_variant(rec.get('name') or '', wanted):
                continue   # a remix or live take the query didn't ask for
            how = ''
            if track is not None:
                how = suits(rec, track)
                if how is None:
                    continue
            elif duration and rec.get('length') and abs(rec['length'] - duration) > LENGTH_TOLERANCE:
                continue
            hits.append((order.index(step), rec, how))
        if not hits:
            return []
        best = min(i for i, _, _ in hits)
        cands = []
        for i, rec, how in hits:
            if i == best:
                c = _candidate(rec)
                c.fit = how
                if track is None or not self.is_blocked(track.title, track.artist, c):
                    cands.append(c)
        # A copy of the playlist's length before a longer version, then the best quality
        cands.sort(key=lambda c: (c.fit != 'longer', c.quality, c.meta.get('added') or 0), reverse=True)
        cands = [c for c in cands if os.path.isfile(c.filename)]
        return cands[:3]


def suits(rec, track):
    """How a copy on the server suits a playlist's song (see matching.length_fit), going by the
    name it was downloaded under and its length, or None when that isn't the song."""
    name = rec.get('name') or ''
    length = rec.get('measured') or rec.get('length')
    if rec.get('origin') not in matching.ELSEWHERE:
        return matching.fit(name, length, track)
    # From SoundCloud or YouTube: the title and uploader it had there
    title, version = matching.title_parts(track.title)
    said = matching.key_words(name.rsplit('.', 1)[0])
    if not matching._span(matching.key_words(title), said) or not version <= set(said):
        return None
    artists = matching.credited(track)
    if artists and not matching.artist_share(artists, set(said) | set(matching.words(rec.get('from') or ''))):
        return None
    return matching.length_fit(length, track.duration, matching.wants_exact(track))


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
              'from': rec.get('from'), 'link': rec.get('link'), 'added': rec.get('added'),
              'name': rec.get('name')},
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

