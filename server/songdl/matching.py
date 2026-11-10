"""Picks the best file for a query out of Soulseek search responses.

Formats are WAV, AIFF, FLAC and MP3; each is searched and ranked on its own.

Ranking, best first:
  1. Relevance: the file looks like the song asked for. Remixes, live takes,
     edits and so on are only relevant when the query asks for them, and the
     length has to agree with what most other copies say.
  2. Availability: the uploader has a free slot or a short queue.
  3. Quality: bit depth, then sample rate for lossless files; bitrate for MP3
     (or any file without a bit depth and sample rate). Anything past
     24-bit / 192 kHz is almost always an upsampled fake, so it ranks as CD
     quality.
  4. Speed: free slot, shorter queue, faster upload speed.
"""

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from . import meta
from .playlist import clean_title

FORMATS = ('wav', 'aiff', 'flac', 'mp3')   # the order the site lists them in
# ...and the formats SoundCloud's and YouTube's audio comes in (see youtube_audio.py), which nobody picks
EXTENSIONS = {'wav': ('.wav',), 'aiff': ('.aiff', '.aif'), 'flac': ('.flac',), 'mp3': ('.mp3',),
              'opus': ('.opus',), 'm4a': ('.m4a',), 'ogg': ('.ogg',)}
LABELS = {'wav': 'WAV', 'aiff': 'AIFF', 'flac': 'FLAC', 'mp3': 'MP3', 'opus': 'Opus', 'm4a': 'M4A', 'ogg': 'OGG'}
LOSSY = {'mp3', 'opus', 'm4a', 'ogg'}
SOURCES = ('soulseek', 'soundcloud', 'youtube')   # where to look; the visitor picks the order they're tried in
SOURCE_LABELS = {'soulseek': 'Soulseek', 'soundcloud': 'SoundCloud', 'youtube': 'YouTube',
                 'local': 'this server'}   # a copy already here (library.py): always looked at first, never picked
ELSEWHERE = SOURCES[1:]   # the ones that aren't Soulseek


def more_after(source, sources):
    """Whether another of `sources` (in the order they're tried) is still to come after `source`."""
    return source in sources and sources.index(source) < len(sources) - 1


# Words that mark a different version of a song than the one usually wanted.
VARIANT_WORDS = {
    'remix', 'remixes', 'remixed', 'rmx', 'mix', 'mixes', 'edit', 'edits', 'reedit', 'live', 'instrumental',
    'instrumentals', 'inst', 'karaoke', 'acapella', 'acapellas', 'acappella', 'cappella', 'dub', 'cover',
    'covers', 'demo', 'rehearsal', 'reprise', 'remake', 'sped', 'slowed', 'reverb', 'nightcore', 'tribute',
    'medley', 'extended', 'vip', 'bootleg', 'bootlegs', 'mashup', 'mashups', 'rework', 'reworks', 'piano',
    'orchestral', 'acoustic', 'unplugged', 'flip', 'refix', 'reboot', 'rerub', 'redo', 'blend', 'transition',
    'stem', 'stems', 'multitrack', 'multitracks', 'vocals',
}
# Words that look like variants but describe the normal song.
NEUTRAL_PHRASES = re.compile(r'\b(album|original|lp|single|main)\s+(version|mix)\b')
# Folders, anywhere in a path, that hold other things than songs as they were released
OTHER_FOLDERS = re.compile(r'\b(?:stems|multitracks?|acapellas|mashups|bootlegs|karaoke|samples|sound\s+effects'
                           r'|sample\s+(?:cds?|packs?)|loop\s+packs?)\b')

PRIVATE = re.compile(r'\bprivate\b', re.I)
# YouTube's own uploads of a label's tracks ("Daft Punk - Topic", "Release - Topic"): no person's channel
TOPIC_CHANNEL = re.compile(r'\s+-\s+Topic$')

DURATION_TOLERANCE = 4  # seconds copies may differ from each other and count as one length
LENGTH_TOLERANCE = 8    # seconds a copy may differ from the playlist's length and be the same recording
LONGER_FACTOR = 2.5     # an extended version may be up to this many times the playlist's length
# A lossless file this small for its length isn't all there: a preview dressed up with a
# full-length header, say. As a share of the uncompressed size (FLAC is usually over half).
MIN_FLAC_RATIO = 0.25
MIN_WAV_RATIO = 0.45


def tokens(text):
    return re.findall(r'[^\W_]+', text.lower())


def search_text(query, fmt):
    """Query sent to Soulseek. Punctuation is dropped so a leading '-' can't
    exclude terms, and the extension narrows results to the wanted format."""
    words = [w for w in tokens(query) if w != fmt]
    return ' '.join(words + [fmt])


@dataclass
class Candidate:
    username: str
    filename: str
    size: int
    length: int | None
    bit_depth: int | None
    sample_rate: int | None
    free_slot: bool
    queue: int
    speed: int
    fmt: str
    bitrate: int | None = None   # as reported by the uploader, kbps
    vbr: bool = False
    variant: bool = False
    outlier: bool = False
    fit: str = ''                # how it suits the playlist's song it was found for (see length_fit)
    measured: int | None = None  # how long it turned out to play, in seconds, once downloaded
    meta: dict = field(default_factory=dict)
    source: str = 'soulseek'     # or 'soundcloud' / 'youtube': username is then the account or channel, url the track
    url: str | None = None       # ...or 'local': a file already on this server, at `filename` (see library.py)
    origin: str | None = None    # for a local file, where it first came from: 'soulseek', 'soundcloud', 'youtube'

    @property
    def key(self):
        """Which remote file this is, so two downloads of it share one fetch."""
        return (self.source, self.url) if self.url else (self.username, self.filename)

    @property
    def basename(self):
        return self.filename.replace('\\', '/').rsplit('/', 1)[-1]

    @property
    def ext(self):
        return '.' + self.basename.rsplit('.', 1)[-1].lower() if '.' in self.basename else ''

    @property
    def kbps(self):
        if self.bitrate and self.fmt in LOSSY:
            return self.bitrate
        return round(self.size * 8 / self.length / 1000) if self.length else None

    @property
    def relevance(self):
        return 0 if self.variant else 1 if self.outlier else 2

    @property
    def available(self):
        return self.free_slot or self.queue <= 10

    @property
    def quality(self):
        if self.fmt not in LOSSY and (self.bit_depth or self.sample_rate):
            depth, rate = self.bit_depth or 16, self.sample_rate or 44100
            if depth > 24 or rate > 192000:
                depth, rate = 16, 44100
            return (depth, rate, 0)
        return (0, 0, self.kbps or 0)

    @property
    def sort_key(self):
        return (self.relevance, self.available, self.quality, self.free_slot, -self.queue, self.speed)

    @property
    def quality_label(self):
        if self.fmt not in LOSSY and self.bit_depth and self.sample_rate:
            return f'{self.bit_depth}-bit / {self.sample_rate / 1000:g} kHz'
        if self.kbps:
            return f'{self.kbps:,} kbps' + (' VBR' if self.vbr and self.fmt in LOSSY else '')
        return 'Unknown quality'

    def describe(self):
        return f'{self.quality_label} {LABELS[self.fmt]} from {self.username}'

    def availability(self):
        slot = 'free slot' if self.free_slot else f'{self.queue} in queue'
        return f'{slot}, {self.speed / 1e6:.1f} MB/s'

    def to_result(self, result_id):
        return {
            'id': result_id,
            **self.meta,
            'filename': self.filename,
            'format': LABELS[self.fmt],
            'duration': self.length,
            'bitrate': self.kbps,
            'sampleRate': self.sample_rate,
            'bitDepth': self.bit_depth,
            'quality': self.quality_label,
            'size': self.size,
        }

    # What is written to the queue file, so a queued song survives a restart
    def to_dict(self):
        return {
            'username': self.username, 'filename': self.filename, 'size': self.size, 'length': self.length,
            'bit_depth': self.bit_depth, 'sample_rate': self.sample_rate, 'free_slot': self.free_slot,
            'queue': self.queue, 'speed': self.speed, 'fmt': self.fmt, 'bitrate': self.bitrate,
            'vbr': self.vbr, 'variant': self.variant, 'outlier': self.outlier, 'fit': self.fit,
            'measured': self.measured, 'meta': self.meta,
            'source': self.source, 'url': self.url, 'origin': self.origin,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Ranking:
    candidates: list  # best first
    format_files: int = 0
    private_files: int = 0
    variant_files: int = 0
    outlier_files: int = 0


def rank(query, fmt, responses):
    exts = EXTENSIONS[fmt]
    wanted = set(tokens(query)) - {fmt}
    out = Ranking([])

    for resp in responses:
        out.private_files += resp.get('lockedFileCount') or 0
        for f in resp.get('files') or []:
            name = f.get('filename') or ''
            if not name.lower().endswith(exts):
                continue
            out.format_files += 1
            if f.get('isLocked') or PRIVATE.search(name):
                out.private_files += 1
                continue
            size = f.get('size') or 0
            length = f.get('length')
            if size < 200_000 or (length is not None and length < 20):
                continue
            if too_small(fmt, size, length, f.get('bitDepth'), f.get('sampleRate')):
                continue
            c = Candidate(
                username=resp['username'],
                filename=name,
                size=size,
                length=length,
                bit_depth=f.get('bitDepth'),
                sample_rate=f.get('sampleRate'),
                free_slot=bool(resp.get('hasFreeUploadSlot')),
                queue=resp.get('queueLength') or 0,
                speed=resp.get('uploadSpeed') or 0,
                fmt=fmt,
                bitrate=f.get('bitRate'),
                vbr=bool(f.get('isVariableBitRate')),
            )
            c.variant = _is_variant(c.filename, wanted)
            c.meta = meta.parse(c.filename)
            out.candidates.append(c)

    _mark_outliers([c for c in out.candidates if not c.variant])
    out.variant_files = sum(c.variant for c in out.candidates)
    out.outlier_files = sum(c.outlier and not c.variant for c in out.candidates)
    out.candidates.sort(key=lambda c: c.sort_key, reverse=True)
    return out


def _is_variant(filename, wanted, longer=False):
    """Whether the file is another version than the one `wanted` (words) asks for. `longer`:
    an extended version will do."""
    parts = filename.replace('\\', '/').split('/')
    wanted = {fold(w) for w in wanted}
    # The file name and its folder (e.g. an album called "Remixes")
    text = NEUTRAL_PHRASES.sub(' ', fold(' '.join(parts[-2:])).rsplit('.', 1)[0])
    found = (set(tokens(text)) & VARIANT_WORDS) - wanted
    if longer:
        found.discard('extended')
    if found:
        return True
    # ...and any folder above that says what's in it isn't songs ("Mashups & Bootlegs", "9 Stems")
    return any(set(tokens(m.group())) - wanted for m in OTHER_FOLDERS.finditer(fold(' '.join(parts[:-1]))))


def too_small(fmt, size, length, bit_depth=None, sample_rate=None):
    """Whether a lossless file is too small to hold the length it says it has."""
    if fmt not in ('flac', 'wav', 'aiff') or not size or not length:
        return False
    full = (bit_depth or 16) * (sample_rate or 44100) * 2 / 8 * length
    if fmt == 'flac':
        return size < full * MIN_FLAC_RATIO
    # Uncompressed: against CD quality, the least a WAV would be, as many don't say what they are
    return size < 16 * 44100 * 2 / 8 * length * MIN_WAV_RATIO


def _mark_outliers(cands):
    """Flags files whose length disagrees with what most copies say."""
    lengths = [c.length for c in cands if c.length]
    if len(lengths) < 3:
        return
    counts = Counter(lengths)
    # The length with the most copies within the tolerance
    consensus = max(counts, key=lambda L: sum(n for l2, n in counts.items() if abs(l2 - L) <= DURATION_TOLERANCE))
    for c in cands:
        if c.length and abs(c.length - consensus) > max(DURATION_TOLERANCE, consensus * 0.02):
            c.outlier = True


# Is this the song a playlist asks for?
#
# Soulseek's search can't be trusted with that: it matches the searched words anywhere in a
# path, folders included ("6am - Peak" finds a song in a folder called "peak time"), and it
# ignores words of one or two letters ("Jansons - U Do" finds everything by Jansons). So a
# copy is only taken for a playlist's song when its own name says it is that song, by one
# of the artists credited, in the version asked for, and its length agrees with the
# playlist's.

_FOLD = str.maketrans({'ø': 'o', 'Ø': 'O', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE', 'ß': 'ss', 'đ': 'd',
                       'Đ': 'D', 'ł': 'l', 'Ł': 'L', 'ı': 'i', '’': "'", '‘': "'", '`': "'", '´': "'"})
_APOSTROPHE = re.compile(r"(?<=\w)'(?=\w)")
SKIP_WORDS = {'the', 'and'}          # "The Bausa" is filed as "Bausa", "Seeing & Believing" as "Seeing and Believing"
_BRACKETS = re.compile(r'[(\[{][^)\]}]*[)\]}]?')   # ...and one left open at the end of a name that was cut off
_ROUND = re.compile(r'\(([^)]*)\)?')
_VERSION_TAIL = re.compile(r'\s+[-–—]\s+')
_CREDITS = re.compile(r'\b(?:feat|ft|featuring)\b.*$')
# Words a version is called by that don't say which one it is
VERSION_GENERIC = {'mix', 'remix', 'rmx', 'edit', 'version', 'original', 'feat', 'ft', 'featuring', 'with'}
# ...and the ones that ask for the short version, so a longer one won't do
SHORT_WORDS = {'edit', 'radio', 'short'}
# ...and the ones that only say how long it is: a copy of that very length needn't repeat them
# ("Comes and Goes - Extended" is often shared as its "(Original Mix)")
LENGTH_WORDS = {'extended', 'radio', 'short', 'long', 'full', 'length'}
# Words in a file name that say nothing about which song it is
NAME_NOISE = {
    'original', 'mix', 'version', 'feat', 'ft', 'featuring', 'with', 'hq', 'premiere', 'premear', 'official',
    'audio', 'video', 'remaster', 'remastered', 'explicit', 'wav', 'flac', 'mp3', 'aiff', 'aif', 'web',
    'vinyl', 'cd', 'bit', 'khz', 'kbps', 'free', 'download', 'dl', 'master', 'mastered', 'single', 'album',
    'main', 'ep', 'lp', 'prod', 'by', 'x', 'vs', 'unknownartist', 'track',
}


def fold(text):
    """Lower case, without accents, and "don't" spelled "dont", so spellings compare."""
    text = unicodedata.normalize('NFKD', (text or '').translate(_FOLD))
    return _APOSTROPHE.sub('', ''.join(ch for ch in text if not unicodedata.combining(ch)).lower())


def words(text):
    """The words of a name, folded, in order."""
    return tokens(fold(text))


def key_words(text):
    """The same, without "the" and "and" (unless that leaves nothing)."""
    all_ = words(text)
    return [w for w in all_ if w not in SKIP_WORDS] or all_


def _span(needle, hay):
    """Where the words `needle` are in `hay` as one phrase: (first, one past the last), or None.
    Words may be split differently ("Mad.Again", "MadAgain"; "don t", "dont")."""
    target = ''.join(needle)
    if not target:
        return None
    for i in range(len(hay)):
        run = ''
        for j in range(i, len(hay)):
            run += hay[j]
            if len(run) >= len(target):
                break
        if run == target:
            return i, j + 1
    return None


def title_parts(title):
    """A playlist title as (the song's name, the words that say which version is asked for):
    "Feel The Friction - Club Angel Remix" is ("Feel The Friction", {"club", "angel"})."""
    text = clean_title(title or '')
    head, *tail = _VERSION_TAIL.split(text, maxsplit=1)
    version = set(words(tail[0])) if tail else set()
    for group in _BRACKETS.findall(head):
        inside = set(words(group))
        if inside & (VARIANT_WORDS | {'version'}):
            version |= inside
    name = ' '.join(_BRACKETS.sub(' ', head).split()) or head
    # (A year or a stray letter doesn't name a version: "Give Me Love - 1977", "Mousse T.'s Extended Mix")
    return name, {w for w in version - VERSION_GENERIC - SKIP_WORDS if len(w) > 1 and not w.isdigit()}


_FEATURING = re.compile(r'\s+(?:feat\.?|ft\.?|featuring)\s+', re.I)


def credited(track):
    """Everyone a playlist credits with the song, the main artist first. "A feat. B" as one
    name (as a text list or a YouTube title gives it) counts as A and as B too."""
    out = []
    for artist in getattr(track, 'artists', None) or (track.artist,):
        for name in [artist] + (_FEATURING.split(artist) if artist and _FEATURING.search(artist) else []):
            if name and name not in out:
                out.append(name)
    return tuple(out)


def asked_words(track):
    """Every word the playlist uses for the song: nothing made of these is unasked for."""
    out = set(words(track.title)) | set(words(track.album))
    for artist in credited(track):
        out |= set(words(artist))
    return out


def wants_exact(track):
    """Whether only a copy of the playlist's length will do: the playlist has another version
    of the song too, or asks for the short one by name."""
    return bool(getattr(track, 'exact', False) or (title_parts(track.title)[1] | set(words(track.title))) & SHORT_WORDS)


def _stem(filename):
    base = filename.replace('\\', '/').rsplit('/', 1)[-1]
    return base.rsplit('.', 1)[0] if '.' in base else base


def _name_fields(stem):
    """The parts of a file name: "03 - Artist - Title", "artist_name-song_title-grp"."""
    stem = stem.replace('–', '-').replace('—', '-')
    tight = [p.replace('_', ' ') for p in re.split(r'-+', stem)]   # "Disclosure-The Face EP-003-Lividup"
    parts = re.split(r'\s+-+\s*|\s*-+\s+|\s*_+\s*', stem) + tight if ' ' in stem else tight
    if ' ' not in stem and '_' not in stem:
        # "Paul-Johnson-Its-a-Boogie-Thang-01-Its-a-Love-Thang": dashes for spaces, the track number between
        parts += [p.replace('-', ' ') for p in re.split(r'-\d{1,3}-', stem)]
    parts = [p.strip() for p in parts if p.strip()]
    return parts + [stem] if len(parts) > 1 else [stem]   # the whole name too: a title can have a dash of its own


def _is_title(field, title, allowed):
    """Whether a part of a file name is the song: the title is in it as a phrase, and nothing
    else is that the playlist didn't say (so "Big Time Charlie" isn't "Big Time", and "Piece
    Of My Heart" isn't "My Heart")."""
    text = _CREDITS.sub('', fold(_BRACKETS.sub(' ', field)))
    hay = [w for w in tokens(text) if w not in SKIP_WORDS]
    span = _span(title, hay)
    if not span:
        return False
    rest = hay[:span[0]] + hay[span[1]:]
    return not any(w not in allowed and not any(ch.isdigit() for ch in w) for w in rest)


def has_artist(path, artists):
    """Whether a path names one of the artists in full, in some folder's name or the file's."""
    parts = [p for p in path.replace('\\', '/').split('/') if p]
    if parts:
        parts[-1] = _stem(parts[-1])
    names = [[w for w in words(p) if w not in SKIP_WORDS] for p in parts]
    everything = {w for n in names for w in n}
    wanted = [key_words(a) for a in artists if key_words(a)]
    if not wanted:
        return True
    return any(any(_span(a, n) for n in names) or set(a) <= everything for a in wanted)


def names_song(filename, track, right_length=False):
    """Whether a file's own name says it is the playlist's song: its title, by one of its
    artists, in the version asked for. `right_length`: it is as long as the playlist says,
    which stands in for words like "extended"."""
    stem = _stem(filename)
    name, version = title_parts(track.title)
    title = key_words(name)
    allowed = asked_words(track) | NAME_NOISE
    if not any(_is_title(f, title, allowed) for f in _name_fields(stem)):
        return False
    if not has_artist(filename, credited(track)):
        return False
    # The version has to be in the file's own name: "Ain't No Game (Clouds Remix)/02 - Ain't No
    # Game.flac" is the original, in the remix's folder. Unless it is the playlist's length:
    # then the folder's word for it will do, and it needn't say "extended" at all
    missing = version - set(words(stem))
    missing -= set(words(track.artist))   # (the main artist's own mix: "Mousse T.'s Extended Mix")
    if missing and right_length:
        folder = filename.replace('\\', '/').rsplit('/', 2)[-2:-1]
        missing -= set(words(folder[0])) if folder else set()
        missing -= LENGTH_WORDS
    return not missing


def odd_bracket(filename, track):
    """Whether a file's name has something in brackets the playlist didn't ask for, which is how
    most remixes are told apart ("(MGR Mike's Disco Groove)"), or a bracket left open because the
    name was cut off, which is where a remix would have said so ("(Subsonic")."""
    allowed = asked_words(track) | NAME_NOISE | SKIP_WORDS
    for group in _ROUND.findall(_stem(filename)):
        if any(w not in allowed and not w.isdigit() for w in words(group)):
            return True
    return False


def length_fit(length, duration, exact=False):
    """How a copy's length suits the playlist's: 'any' (the playlist doesn't say), 'unknown'
    (the copy doesn't), 'exact', 'longer' (an extended version, when one will do), or None for
    a length that can't be the song: shorter, or far too long."""
    if not duration:
        return 'any'
    if not length:
        return 'unknown'
    off = length - duration
    if abs(off) <= LENGTH_TOLERANCE:
        return 'exact'
    if off < 0 or exact or length > duration * LONGER_FACTOR:
        return None
    return 'longer'


def fit(filename, length, track):
    """How a Soulseek file suits a playlist's song (see length_fit), or None when it isn't it."""
    exact = wants_exact(track)
    how = length_fit(length, track.duration, exact)
    if how is None or not names_song(filename, track, how == 'exact'):
        return None
    if _is_variant(filename, asked_words(track), longer=not exact):
        return None
    # Something unexplained in brackets is only let through by the right length
    if how != 'exact' and odd_bracket(filename, track):
        return None
    return how


@dataclass
class Want:
    """What a download has to live up to once it's here: the playlist's length for the song,
    and whether only that length will do (see wants_exact)."""
    duration: int | None = None
    exact: bool = False

    @classmethod
    def of(cls, track):
        return cls(track.duration, wants_exact(track)) if track.duration else None


def mark_versions(tracks):
    """Marks the songs a playlist has in more than one version ("Comes and Goes" and "Comes
    and Goes - Extended"): each must be the length the playlist gives, or one would be saved
    as the other."""
    groups = {}
    for t in tracks:
        key = (tuple(key_words(t.artist)), tuple(key_words(title_parts(t.title)[0])))
        groups.setdefault(key, []).append(t)
    for group in groups.values():
        if len(group) > 1:
            for t in group:
                t.exact = True
    return tracks


def artist_share(artists, text_words):
    """Whether at least half the words of one of the artists' names are among the words."""
    for artist in artists:
        name = set(key_words(artist))
        if name and len(name & text_words) >= math.ceil(len(name) / 2):
            return True
    return False
