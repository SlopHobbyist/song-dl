"""Reads a link from a music service. A playlist or album becomes a CSV of its
songs (the page offers it to save, and scans or imports it like any uploaded
file); a single song becomes the search for it.

Each service has a module with find(text) -> (kind, id) or None, where kind is
'playlist', 'album' or 'song', and a reader class whose read(kind, id) gives a
Playlist (anything with name, owner, rows, csv() and unavailable) or a
catalog.Song. A playlist read must have every song on it: a reader that gets
fewer than the service says are there refuses, rather than hand back a
shorter CSV.

Share links that only redirect (spotify.link/…, link.deezer.com/s/…) are
followed first. Only the hosts in SHORT are followed, so a link can't send the
server anywhere else.
"""

import asyncio
import logging
import re
from dataclasses import dataclass

import aiohttp

from . import amazon, apple, beatport, deezer, pandora, qobuz, spotify, tidal, youtube
from .catalog import USER_AGENT, ServiceError, Song
from .errors import UserError

log = logging.getLogger('songdl')

# key, module (find and the reader), the hosts its links are on
SERVICES = [
    ('spotify', spotify, spotify.Spotify, r'(?:open|play)\.spotify\.com|spotify:'),
    ('youtube', youtube, youtube.YouTube, r'(?:www\.|m\.|music\.)?youtube\.com|youtu\.be'),
    ('apple', apple, apple.AppleMusic, r'(?:music|itunes|embed\.music|geo\.music)\.apple\.com'),
    ('amazon', amazon, amazon.AmazonMusic, r'music\.amazon\.[a-z.]{2,6}|(?:www\.)?amazon\.[a-z.]{2,6}/music'),
    ('deezer', deezer, deezer.Deezer, r'(?:www\.)?deezer\.com'),
    ('tidal', tidal, tidal.Tidal, r'(?:www\.|listen\.|embed\.)?tidal\.com'),
    ('qobuz', qobuz, qobuz.Qobuz, r'(?:www\.|open\.|play\.)?qobuz\.com'),
    ('pandora', pandora, pandora.Pandora, r'(?:www\.)?pandora\.com'),
    ('beatport', beatport, beatport.Beatport, r'(?:www\.)?beatport\.com'),
]

# Share links that redirect to a service's own link
SHORT = re.compile(
    r'\bhttps?://(?:spotify\.link|spotify\.app\.link|link\.deezer\.com|deezer\.page\.link|dzr\.page\.link'
    r'|pandora\.app\.link)'
    r'/[^\s"\'<>]+', re.I)
SHORT_TIMEOUT = 10

NOT_A_LINK = ('That isn\'t a playlist, album or song link Song-DL can read. Paste a link from Spotify, '
              'Apple Music, Amazon Music, YouTube or YouTube Music, Deezer, Tidal, Qobuz, Pandora or Beatport.')


@dataclass
class Link:
    service: str
    name: str        # the service's name, as the page shows it
    kind: str        # 'playlist', 'album' or 'song'
    id: str


_KNOWN = re.compile(r'(?:^|[/.\s])(?:' + '|'.join(host for *_, host in SERVICES) + r')', re.I)


def find(text):
    """The Link in some text, or None. No network: short links are left to resolve()."""
    text = str(text or '').strip()
    for key, module, reader, _ in SERVICES:
        if found := module.find(text):
            return Link(key, reader.name, *found)
    return None


def known(text):
    """Whether the text links to a service in SERVICES or a short link, readable or not."""
    text = str(text or '')
    return bool(_KNOWN.search(text) or SHORT.search(text))


def check(text):
    """The Link, or for a short link None, to be resolved; otherwise a UserError
    that says what's wrong with the link."""
    if link := find(text):
        return link
    if SHORT.search(str(text or '')):
        return None
    for _, module, _, _ in SERVICES:
        for pattern, message in getattr(module, 'UNSUPPORTED', ()):
            if pattern.search(str(text or '')):
                raise UserError(message)
    if known(text):
        raise UserError('Song-DL can read playlist, album and song links, but not artists, channels, '
                        'podcasts or stations. Paste a link to a playlist, album or song instead.')
    raise UserError(NOT_A_LINK)


async def resolve(text):
    """Follows a short share link to the link it stands for."""
    m = SHORT.search(str(text or ''))
    if not m:
        raise UserError(NOT_A_LINK)
    url = m.group(0)
    timeout = aiohttp.ClientTimeout(total=SHORT_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': USER_AGENT}) as http:
            async with http.get(url, max_redirects=6) as res:
                final = str(res.url)
                body = await res.content.read(256 * 1024) if not find(final) else b''
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.info('short link %s: %r', url, e)
        raise UserError('Couldn\'t follow that share link. Paste the full link instead.', 502)
    # Some answer with a page that sends the browser on, so look for the link in it too
    link = find(final) or find(body.decode('utf-8', 'replace').replace('\\/', '/'))
    if not link:
        log.info('short link %s led to %s, no link found', url, final)
        raise UserError('That share link doesn\'t lead to a playlist, album or song. Paste the full link instead.')
    log.info('short link %s -> %s %s %s', url, link.service, link.kind, link.id)
    return link


class Readers:
    """One reader per service, kept for the server's life (they keep tokens)."""

    def __init__(self):
        self.readers = {key: reader() for key, _, reader, _ in SERVICES}

    async def read(self, link):
        """A Playlist for a playlist or album link, a Song for a song link."""
        reader = self.readers[link.service]
        try:
            return await reader.read(link.kind, link.id)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning('%s %s %s: %r', link.service, link.kind, link.id, e)
            raise UserError(f'Couldn\'t reach {link.name}. Please try again in a minute.', 502)
        except ServiceError as e:
            log.warning('%s %s %s: %s', link.service, link.kind, link.id, e)
            raise UserError(f'{link.name} answered in a way Song-DL doesn\'t understand, so the {link.kind} '
                            'couldn\'t be read. Try again later, or export it to CSV another way.', 502)


__all__ = ['Link', 'Readers', 'Song', 'check', 'find', 'known', 'resolve']
