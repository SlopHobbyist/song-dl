"""Best-effort title, artist and album from a Soulseek file path.

Soulseek results carry no tags, only paths like
    @@share\\Music\\Daft Punk\\(2001) Discovery\\03 - Digital Love.flac
so everything here is guessing from common naming schemes.
"""

import re

# Folder names that never name an artist or album.
GENERIC_DIRS = {
    'music', 'musique', 'musik', 'mp3', 'flac', 'wav', 'audio', 'share', 'shared', 'shares', 'soulseek',
    'downloads', 'download', 'complete', 'completed', 'library', 'albums', 'album', 'singles', 'eps',
    'artists', 'compilations', 'media', 'my music', 'soulseek shared folder', 'lossless', 'hi res',
    'hires', 'cd', 'vinyl', 'studio', 'studio album', 'studio albums', 'various', 'various artists',
    'va', 'tagged', 'new', 'misc',
}
DISC = re.compile(r'^(?:\d+"?\s*)?(cd|disc|disk|side|vinyl)\s*-?\s*[\dA-D]*$', re.I)
# Track number opening a file name: "03 ", "03. ", "1-03 ", "A3. ", "(04) ", "Track 03 - "
TRACK_PREFIX = re.compile(
    r'^\s*(?:(?:disc|cd)\s*\d+\s*)?(?:track|trk)?\s*'
    r'(?:\(\d{1,3}\)|[A-D]\d{1,2}|\d{1,2}[-.]\d{1,3}|(?!19|20)\d{4}|\d{1,3})(?:\s*[-._)\]]+\s*|\s+)', re.I)
# Later in a name a track number needs punctuation, so "99 Problems" stays intact
INNER_TRACK = re.compile(r'^(?:\d{1,2}-\d{2,3}|\d{1,3}[.)])\s*')
YEAR_PREFIX = re.compile(r'^[\[(]?(19|20)\d\d[\])]?\s*[-.]?\s*(?=\S)')
FORMAT_TAG = re.compile(
    r'\s*[\[({][^\])}]*\b(flac|wav|mp3|24|16|\d+\s*bits?|bit|khz|kbps|web|cd|vinyl|lossless|hi-?res|\d{6,})\b'
    r'[^\])}]*[\])}]', re.I)
FORMAT_FIELD = re.compile(r'^(flac|wav|\d+\s*bits?|\d+(\.\d+)?\s*khz)$', re.I)
TRAILING_FORMAT = re.compile(r'\s+-\s+(web|cd|vinyl|flac|\d+\s*bits?)\b[^-]*$', re.I)  # "Album - WEB FLAC"
GROUP_SUFFIX = re.compile(r'(?<=\S)-[a-z0-9]{2,6}$')  # scene release group: "Title-cr"
TRAILING_YEAR = re.compile(r'(?<=\S)\s+-\s+[\[(]?(19|20)\d\d[\])]?$')  # "Album - (2001)"
LEADING_TAG = re.compile(r'^\[[^\]]*\]\s+')  # "[M] Album", "[Orpheus] Artist"


def _norm(name):
    return re.sub(r'[\W_]+', ' ', name.lower()).strip()


def _dashes(name):
    return name.replace('–', '-').replace('—', '-')


def _capitalize(text):
    """Capitalizes all-lowercase names like "digital love" from scene releases."""
    if text and text == text.lower():
        return re.sub(r'(^|[\s(\[])(\w)', lambda m: m[1] + m[2].upper(), text)
    return text


def _tidy_dir(name):
    name = _dashes(name)
    if ' ' not in name:
        name = name.replace('_', ' ')
    name = TRAILING_FORMAT.sub('', FORMAT_TAG.sub('', name)).strip()
    return TRAILING_YEAR.sub('', name)


def _clean_album(name, artist=None):
    name = LEADING_TAG.sub('', YEAR_PREFIX.sub('', _tidy_dir(name)))
    if artist and name.lower().startswith(artist.lower()):
        name = name[len(artist):].lstrip(' -_')
    name = re.sub(r'^(album|ep|single)\s*-\s*', '', name, flags=re.I)
    name = YEAR_PREFIX.sub('', name)
    return name.strip(' -_.') or None


def _is_generic(name):
    if name.startswith('@@') or DISC.match(name.strip()):
        return True
    n = _norm(TRACK_PREFIX.sub('', _tidy_dir(name)))
    return len(n) <= 1 or re.sub(r'\d+', '', n).strip() in GENERIC_DIRS


def _fields(stem):
    """Splits a file name like "Artist_Album_03_Title" or "03 - Artist - Title"
    into its named parts, dropping track numbers, disc numbers and format tags."""
    stem = _dashes(stem)
    spaced = ' ' in stem
    if spaced:
        stem = re.sub(r'\s*_\s*', ' - ', stem)  # "Artist_Album_03_Title"
    elif '_' in stem:
        stem = stem.replace('_', ' ')  # "03-daft_punk-digital_love-uau"
    elif stem.count('.') >= 2:
        stem = stem.replace('.', ' ')  # "06..Digital.Love"
    stem = TRACK_PREFIX.sub('', FORMAT_TAG.sub('', stem))
    if stem != stem.lower():
        stem = GROUP_SUFFIX.sub('', stem)

    sep = r'\s+-\s+' if re.search(r'\s-\s', stem) or (spaced and stem.count('-') < 2) else r'\s*-\s*'
    fields = [f.strip(' .') for f in re.split(sep, stem)]
    if not spaced and len(fields) > 2 and re.fullmatch(r'[a-z0-9]{2,6}', fields[-1]):
        fields.pop()  # scene release group: "-uau"
    fields = [INNER_TRACK.sub('', f) for f in fields
              if f and not FORMAT_FIELD.match(f) and not DISC.match(f)]
    if len(fields) >= 3:
        # "Artist - Album - 03 Title"
        fields[-1] = re.sub(r'^\d{1,3}\s+(?=\S)', '', fields[-1])
    named = [f for f in fields if not re.fullmatch(r'[A-D]?\d{1,4}', f)]
    return named or fields[-1:]


def parse(filename):
    parts = [p for p in filename.replace('\\', '/').split('/') if p]
    stem = parts[-1].rsplit('.', 1)[0]
    dirs = [d for d in parts[:-1] if not _is_generic(d)]
    dir_names = {_norm(d) for d in dirs}

    fields = _fields(stem) or [stem]
    title, artist = fields[-1], None
    if len(fields) >= 2:
        artist = fields[0]
        # "Title - Artist": the artist is usually also a folder name
        if _norm(title) in dir_names and _norm(artist) not in dir_names:
            title, artist = artist, title
    m = re.match(r'^\[([^\]]+)\]\s*(.+)$', title)  # "[Artist] Title"
    if m:
        artist, title = artist or m[1], m[2]

    album = None
    if dirs:
        raw = LEADING_TAG.sub('', YEAR_PREFIX.sub('', _tidy_dir(dirs[-1])))
        if ' ' not in dirs[-1] and dirs[-1].count('-') >= 2:
            # Scene release folder: "Artist-Album-REISSUE-2LP-FLAC-2021-GROUP"
            head, raw = raw.split('-')[:2]
            artist = artist or head
        if not artist:
            head = raw.split(' - ', 1)[0].strip()
            # ...unless the head is the song title, as in a single's folder "Title - Subtitle"
            if (' - ' in raw and not re.fullmatch(r'\d{4}', head) and head.count('(') == head.count(')')
                    and not _norm(title).startswith(_norm(head))):
                artist = head  # "Artist - Album"
            elif len(dirs) >= 2:
                artist = (_clean_album(dirs[-2]) or '').split(' - ')[0] or None  # Artist\Album
        album = _clean_album(raw, artist)

    if album and artist and _norm(album) == _norm(artist):
        album = None
    return {'title': _capitalize(title), 'artist': _capitalize(artist), 'album': _capitalize(album)}
