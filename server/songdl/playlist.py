"""Reads a playlist exported as CSV into a list of tracks.

Every service names its columns differently (Spotify exports use "Track Name"
and "Artist Name(s)", Exportify and TuneMyMusic use "title" or "name" and
"artists", and so on), so columns are found by looking their heading up in a
table of known names. Only the song name is required; the artist makes the
search much more accurate.
"""

import csv
import io
import re
from dataclasses import dataclass

from .errors import UserError

MAX_TRACKS = 300

# Normalised headings (lowercase letters and digits only), most specific first.
TITLE = ('title', 'trackname', 'songname', 'tracktitle', 'songtitle', 'track', 'song', 'name')
ARTIST = ('artist', 'artists', 'artistname', 'artistnames', 'trackartist', 'trackartists',
          'songartist', 'performer', 'performers', 'albumartist')
ALBUM = ('album', 'albumname', 'albumtitle')
DURATION = ('duration', 'durationms', 'durationsec', 'durationseconds', 'length', 'time')

# "(feat. X)", "[with X]" and "- feat. X" don't help a search and often break it
CREDIT = re.compile(r'\s*[(\[]\s*(?:feat|ft|featuring|with|w/|prod)\b[^)\]]*[)\]]', re.I)
CREDIT_TAIL = re.compile(r'\s+-\s+(?:feat|ft|featuring|with|prod)\b.*$', re.I)
# "- Remastered 2011", "(Single Version)" and similar describe the same song
VERSION = re.compile(r'\s+-\s+[^-]*\b(?:remaster(?:ed)?|(?:single|album|mono|stereo) version)\b.*$'
                     r'|\s*[(\[][^)\]]*\b(?:remaster(?:ed)?|(?:single|album|mono|stereo) version)\b[^)\]]*[)\]]', re.I)


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
    """"A; B; C" and "A, B" both mean several artists; searching for the first is enough."""
    return re.split(r'[;,\n]', artists, maxsplit=1)[0].strip()


def parse(raw: bytes):
    """Returns (tracks, skipped) where skipped counts rows without a song name."""
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

    if not tracks:
        raise UserError('Couldn\'t find any songs in that file.')
    if len(tracks) > MAX_TRACKS:
        raise UserError(f'That playlist has {len(tracks)} songs. The limit is {MAX_TRACKS} per upload; '
                        'split it into smaller files.')
    return tracks, skipped
