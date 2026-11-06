"""Reads a playlist exported as CSV, or a plain text list, into a list of tracks.

Every service names its columns differently (Spotify exports use "Track Name"
and "Artist Name(s)", Exportify and TuneMyMusic use "title" or "name" and
"artists", and so on), so columns are found by looking their heading up in a
table of known names. Only the song name is required; the artist makes the
search much more accurate.

A text file (parse_text) has one song per line, ideally "Artist - Song".
"""

import csv
import io
import re
from dataclasses import dataclass

from .errors import UserError

MAX_TRACKS = 300   # a browser scan; admins' imports (imports.py) take far more

# Normalised headings (lowercase letters and digits only), most specific first.
TITLE = ('title', 'trackname', 'songname', 'tracktitle', 'songtitle', 'track', 'song', 'name')
ARTIST = ('artist', 'artists', 'artistname', 'artistnames', 'trackartist', 'trackartists',
          'songartist', 'performer', 'performers', 'albumartist')
ALBUM = ('album', 'albumname', 'albumtitle')
DURATION = ('duration', 'durationms', 'durationsec', 'durationseconds', 'length', 'time')

# "(feat. X)", "[with X]" and "- feat. X" don't help a search and often break it
CREDIT = re.compile(r'\s*[(\[]\s*(?:feat|ft|featuring|with|w/|prod)\b[^)\]]*[)\]]', re.I)
CREDIT_TAIL = re.compile(r'\s+-\s+(?:feat|ft|featuring|with|prod)\b.*$', re.I)
# "- Remastered 2011", "(Single Version)", Beatport's "(Original Mix)" and similar describe the same song
VERSION = re.compile(r'\s+-\s+[^-]*\b(?:remaster(?:ed)?|(?:single|album|mono|stereo|original) version|original mix)\b.*$'
                     r'|\s*[(\[][^)\]]*\b(?:remaster(?:ed)?|(?:single|album|mono|stereo|original) version|original mix)\b'
                     r'[^)\]]*[)\]]', re.I)


@dataclass
class Track:
    title: str
    artist: str            # main artist only
    album: str = ''
    duration: int | None = None   # seconds

    @property
    def query(self):
        title = clean_title(self.title)
        return f'{self.artist} {title}'.strip()

    @property
    def title_query(self):
        return clean_title(self.title)

    @property
    def label(self):
        return f'{self.artist} - {self.title}' if self.artist else self.title


def clean_title(title):
    title = CREDIT.sub('', title)
    title = CREDIT_TAIL.sub('', title)
    title = VERSION.sub('', title)
    return ' '.join(title.split()) or title


def _norm(heading):
    return re.sub(r'[^a-z0-9]', '', heading.lower())


def _find(headings, names):
    for name in names:
        if name in headings:
            return headings.index(name)
    return None


def _decode(raw):
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('cp1252', errors='replace')


def _seconds(value, heading):
    value = value.strip()
    if not value:
        return None
    if ':' in value:
        try:
            secs = 0
            for part in value.split(':'):
                secs = secs * 60 + int(part)
            return secs
        except ValueError:
            return None
    try:
        n = float(value)
    except ValueError:
        return None
    if heading == 'durationms' or n >= 10000:   # no song is 10,000 seconds long
        n /= 1000
    return round(n) if n > 0 else None


def _main_artist(artists):
    """"A; B; C" and "A, B" both mean several artists; searching for the first is enough.
    Some names have a comma of their own, though: "Earth, Wind & Fire", "Crosby, Stills,
    Nash & Young" and "Tyler, The Creator" are kept whole."""
    first = re.split(r'[;\n]', artists, maxsplit=1)[0]
    parts = [p.strip() for p in first.split(',')]
    name = parts[0]
    for i, part in enumerate(parts[1:], 1):
        if re.match(r'the\b', part, re.I) and len(part.split()) <= 3:
            name += ', ' + part        # Tyler, The Creator
            continue
        # Single words leading up to an "X & Y" or "X and Y": one band
        if all(len(p.split()) == 1 for p in parts[:i]) and \
                any(re.search(r'\s(?:&|and)\s', p, re.I) for p in parts[i:i + 3]):
            end = next(j for j in range(i, len(parts)) if re.search(r'\s(?:&|and)\s', parts[j], re.I))
            if all(len(p.split()) == 1 for p in parts[i:end]):
                name = ', '.join(parts[:end + 1])
        break
    return name.strip()


def _finish(tracks, skipped, limit, too_many):
    if not tracks:
        raise UserError('Couldn\'t find any songs in that file.')
    if len(tracks) > limit:
        raise UserError((too_many or 'That playlist has {n} songs. A scan can have up to {limit}; '
                         'split it into smaller files.').format(n=len(tracks), limit=limit))
    return tracks, skipped


# "1. ", "12) ", "- ", "* ", "• " in front of a line of a list
LIST_MARK = re.compile(r'^(?:\d{1,4}[.)]|[-*•–])\s+')


def parse_text(raw: bytes, limit=MAX_TRACKS, too_many=None):
    """A plain text list, one song per line: "Artist - Song", or just the song name.
    Returns (tracks, skipped) like parse; skipped counts lines that were only a link."""
    from .youtube import split   # its title splitter also drops "(Official Video)" and the like
    text = _decode(raw)
    if not text.strip():
        raise UserError('That file is empty.')
    tracks, seen, skipped = [], set(), 0
    for line in text.splitlines():
        line = LIST_MARK.sub('', ' '.join(line.split()))
        if not line or line.startswith('#'):
            continue
        if re.match(r'(?:https?://|www\.)\S*$', line, re.I):
            skipped += 1   # a bare link names no song
            continue
        artist, title = split(line, '')
        artist, title = _main_artist(artist), title or line
        key = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        tracks.append(Track(title, artist))
    return _finish(tracks, skipped, limit, too_many)


def parse(raw: bytes, limit=MAX_TRACKS, too_many=None):
    """Returns (tracks, skipped) where skipped counts rows without a song name.
    More than `limit` songs is refused with `too_many` (formatted with n and limit)."""
    text = _decode(raw)
    if not text.strip():
        raise UserError('That file is empty.')
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    try:
        rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    except csv.Error:
        raise UserError('That doesn\'t look like a CSV file.')

    # The heading row is the first one (within the first few) that has a song name column
    header = None
    for i, row in enumerate(rows[:5]):
        norm = [_norm(c) for c in row]
        if _find(norm, TITLE) is not None:
            header, body = norm, rows[i + 1:]
            break
    if header is None:
        raise UserError('Couldn\'t find a song name column. The first row of the CSV needs headings '
                        'like "title" (or "name") and "artist".')

    t_col, a_col = _find(header, TITLE), _find(header, ARTIST)
    b_col, d_col = _find(header, ALBUM), _find(header, DURATION)

    def cell(row, col):
        return row[col].strip() if col is not None and col < len(row) else ''

    tracks, seen, skipped = [], set(), 0
    for row in body:
        title = cell(row, t_col)
        if not title:
            skipped += 1
            continue
        artist = _main_artist(cell(row, a_col))
        key = (artist.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        tracks.append(Track(title, artist, cell(row, b_col),
                            _seconds(cell(row, d_col), header[d_col]) if d_col is not None and d_col < len(header) else None))
    return _finish(tracks, skipped, limit, too_many)
