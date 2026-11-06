"""What the music service readers (deezer.py, apple.py, tidal.py and so on) hand
back: a playlist or album as rows of a CSV, or a single song to search for.
links.py picks the reader for a link.

Every reader's CSV has the same columns, named so playlist.parse finds them.
spotify.py and youtube.py keep columns of their own (a Track URI, a Video URL).
"""

import csv
import io
from dataclasses import dataclass, field

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/130.0 Safari/537.36')

COLUMNS = ['Track Name', 'Artist Name(s)', 'Album Name', 'Duration (ms)', 'ISRC', 'URL']


class ServiceError(Exception):
    """A service answered in a way its reader doesn't understand."""


@dataclass
class Playlist:
    id: str
    name: str
    owner: str
    rows: list = field(default_factory=list)   # dicts keyed by COLUMNS
    unavailable: int = 0                       # songs the service listed but no longer has

    def csv(self):
        out = io.StringIO()
        writer = csv.DictWriter(out, COLUMNS, lineterminator='\r\n', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(self.rows)
        # The BOM makes Excel read the file as UTF-8
        return '﻿' + out.getvalue()


def row(title, artists, album='', ms=None, isrc='', url=''):
    """One CSV row. Artists (a list) are joined with ";" so a name with a comma of
    its own ("Earth, Wind & Fire") stays whole."""
    if isinstance(artists, str):
        artists = [artists]
    return {
        'Track Name': ' '.join(str(title or '').split()),
        'Artist Name(s)': '; '.join(dict.fromkeys(' '.join(a.split()) for a in artists if a and a.strip())),
        'Album Name': ' '.join(str(album or '').split()),
        'Duration (ms)': int(ms) if ms else '',
        'ISRC': isrc or '',
        'URL': url or '',
    }


@dataclass
class Song:
    title: str
    artist: str                 # every artist, joined with ";"
    album: str = ''
    duration: int | None = None   # seconds
    video: str | None = None      # a YouTube video id: the fallback downloads that video
    extra: dict = field(default_factory=dict)   # more fields for the page (a video's title and channel)
