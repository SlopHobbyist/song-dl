"""Turns a YouTube (or YouTube Music) playlist link into a CSV of its songs,
and a single video's link into the song to search for.

yt-dlp lists the playlist without downloading anything, following YouTube's
pages to the end, so the CSV has every video: public and unlisted playlists
both work, since anyone with the link can see them. Deleted and private videos
are kept as rows with only their address, so the CSV is still the whole
playlist; a scan skips them as rows without a song name.

YouTube has no artist and song fields, only a video title and a channel, so
those are split here: "Artist - Song (Official Video)", "Song" from an
"Artist - Topic" channel, 'Artist "Song"', and so on. See split().
"""

import asyncio
import csv
import html
import io
import json
import logging
import re
from dataclasses import dataclass, field

import aiohttp

from .catalog import Song
from .errors import UserError

log = logging.getLogger('songdl')

MAX_ITEMS = 5_000     # YouTube's own limit on a playlist
TIMEOUT = 240         # seconds; about 1 per 100 videos
READS = 2             # playlists read at once

# youtube.com/playlist?list=, watch?v=…&list=, youtu.be/…?list=, music.youtube.com/…
PLAYLIST = re.compile(
    r'(?:^|[/.\s])(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/\S*?[?&]list=([A-Za-z0-9_-]{10,64})')
# watch?v=, youtu.be/, shorts/, live/, embed/; also on m. and music.
VIDEO = re.compile(
    r'(?:^|[/.\s])(?:(?:www|m|music)\.)?(?:youtube\.com/(?:watch\?(?:\S*?&)?v=|shorts/|live/|embed/)|youtu\.be/)'
    r'([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])')
OEMBED_URL = 'https://www.youtube.com/oembed'
WATCH_URL = 'https://www.youtube.com/watch?v={id}'
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
              'Chrome/130.0 Safari/537.36')
META_TITLE = re.compile(r'<meta name="title" content="([^"]*)"')
META_CHANNEL = re.compile(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"')
VIDEO_NOT_FOUND = 'YouTube couldn\'t find that video. Check the link, and that the video isn\'t private.'

COLUMNS = ['Video URL', 'Track Name', 'Artist Name(s)', 'Album Name', 'Duration (ms)', 'Video Title', 'Channel']

NOT_FOUND = ('YouTube couldn\'t find that playlist. Check the link, and that the playlist isn\'t '
             'private: Song-DL can only read public and unlisted playlists.')
MIX = ('That\'s a YouTube Mix, which YouTube makes up as you listen, so it can\'t be read. '
       'Save the songs to a playlist first.')


def playlist_id(text):
    """The playlist ID in a YouTube link, or None."""
    m = PLAYLIST.search(str(text or ''))
    return m.group(1) if m else None


def video_id(text):
    """The video ID in a link to a single YouTube video, or None. A video link
    that names a playlist too is the playlist's (see playlist_id)."""
    if playlist_id(text):
        return None
    m = VIDEO.search(str(text or ''))
    return m.group(1) if m else None


def find(text):
    """(kind, id) for a YouTube playlist or video link, or None."""
    if pid := playlist_id(text):
        return 'playlist', pid
    if vid := video_id(text):
        return 'song', vid
    return None


# Split title  ------------------------------------------------------------

DASH = re.compile(r'\s+[-–—―]+\s+|\s+-\s*|\s*-\s+(?=[A-Z0-9"\'(\[])')
QUOTED = re.compile(r'^(?P<artist>[^"“”]+?)\s*[:\-–—]?\s*["“](?P<title>[^"”]+)["”]')
# Words in a bracket, or after a dash or bar at the end, that only describe the video
NOISE_WORDS = (r'official|video|audio|lyrics?|lyric video|visuali[sz]er|music video|m/?v|hd|hq|4k|'
               r'1080p|720p|full song|full album|explicit|clean|clip officiel|videoclip|video clip|'
               r'performance video|animated video|subtitulado|legendado|color(?:ized)?|high quality|'
               r'upscaled|full hd')
NOISE_BRACKET = re.compile(r'\s*[(\[【][^)\]】]*\b(?:' + NOISE_WORDS + r')\b[^)\]】]*[)\]】]', re.I)
NOISE_TAIL = re.compile(r'(?:\s+(?:[-|–—/]\s*)?(?:\(?\s*(?:official\b.*|(?:hd|hq|4k)\s*$|lyrics?\s*$)|[|/•].*$)'
                        r'|,?\s*full hd\b.*$)', re.I)
# What follows "Song - " when it names a recording rather than a song
VERSION_ONLY = re.compile(r'^(?:[\w\'’.]+\s+){0,3}?(?:\d{4}\s+)?(?:remaster(?:ed)?|version|edit|mix|live|mono|stereo|'
                          r'acoustic|demo|instrumental|single|radio edit|bonus track)(?:\s+[\w\'’.]+){0,3}$', re.I)
YEAR = re.compile(r'\s*[(\[]\d{4}[)\]]')
HASHTAGS = re.compile(r'\s+#\w+')
FEAT = re.compile(r'\s+(?:feat\.?|ft\.?|featuring|feat:)\s.*$', re.I)
TOPIC = re.compile(r'\s+-\s+Topic$')
VEVO = re.compile(r'VEVO$')
CHANNEL_NOISE = re.compile(r'\s*\b(?:official|music|tv|channel)\b\s*', re.I)
CAMEL = re.compile(r'(?<=[a-z])(?=[A-Z])')


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _clean(text):
    text = HASHTAGS.sub('', text)
    for _ in range(3):   # "(Official Video) [HD]" has two
        text = NOISE_BRACKET.sub('', text)
    text = NOISE_TAIL.sub('', text)
    text = YEAR.sub('', text)
    return ' '.join(text.strip(' -–—|:,').split())


def _channel_artist(channel):
    """The artist a channel stands for: "Daft Punk - Topic", "DaftPunkVEVO", "Queen Official",
    "TheCranberriesTV"."""
    if TOPIC.search(channel):
        return TOPIC.sub('', channel)
    name = VEVO.sub('', channel)
    if ' ' not in name:
        name = CAMEL.sub(' ', name)   # "TheCranberriesTV" -> "The Cranberries TV"
    return ' '.join(CHANNEL_NOISE.sub(' ', name).split()) or channel


def split(title, channel):
    """(artist, song) for a video, as best the title and channel say."""
    title = ' '.join((title or '').split())
    channel = ' '.join((channel or '').split())
    from_channel = _channel_artist(channel)
    if TOPIC.search(channel):
        # YouTube Music's own uploads: the title is the song and nothing else
        return from_channel, _clean(title)

    m = QUOTED.match(title)
    if m and len(m.group('artist').split()) <= 6:
        return FEAT.sub('', m.group('artist').strip()), _clean(m.group('title'))

    cleaned = _clean(title)
    parts = DASH.split(cleaned, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), _clean(parts[1])
        if VERSION_ONLY.match(right):
            # "Dreams - 2004 Remaster": the part after the dash is which recording, not the song
            return from_channel, cleaned
        # "Song - Artist", uploaded by the artist
        if _norm(right) == _norm(from_channel) and _norm(left) != _norm(from_channel):
            left, right = right, left
        return FEAT.sub('', left), FEAT.sub('', right)
    # "Metallica: Enter Sandman", from Metallica
    if ': ' in cleaned:
        left, right = cleaned.split(': ', 1)
        if _norm(left) == _norm(from_channel):
            return left, FEAT.sub('', right)
    # "Black Velvet by Alannah Myles"
    m = re.match(r'(.+?)\s+by\s+(.+)$', cleaned, re.I)
    if m and _norm(m.group(2)) == _norm(from_channel):
        return m.group(2), m.group(1)
    return from_channel, FEAT.sub('', cleaned)


def _audio_only(title, channel):
    """True when the video is just the recording, so its length is the song's."""
    return bool(TOPIC.search(channel or '')) or bool(re.search(r'\baudio\b', title or '', re.I)) \
        and not re.search(r'\bvideo\b', title or '', re.I)


@dataclass
class Playlist:
    id: str
    name: str
    owner: str
    rows: list = field(default_factory=list)
    unavailable: int = 0

    def csv(self):
        out = io.StringIO()
        writer = csv.DictWriter(out, COLUMNS, lineterminator='\r\n')
        writer.writeheader()
        writer.writerows(self.rows)
        return '﻿' + out.getvalue()


def _row(entry):
    title, channel = entry.get('title') or '', entry.get('channel') or entry.get('uploader') or ''
    url = entry.get('url') or f'https://www.youtube.com/watch?v={entry.get("id", "")}'
    if not title or not channel or title in ('[Deleted video]', '[Private video]'):
        return {'Video URL': url, 'Track Name': '', 'Artist Name(s)': '', 'Album Name': '',
                'Duration (ms)': '', 'Video Title': title, 'Channel': channel}
    artist, song = split(title, channel)
    duration = entry.get('duration')
    return {
        'Video URL': url,
        'Track Name': song or title,
        'Artist Name(s)': artist,
        'Album Name': '',
        # A music video's length is often not the song's, so only an audio upload's is given
        'Duration (ms)': int(duration * 1000) if duration and _audio_only(title, channel) else '',
        'Video Title': title,
        'Channel': channel,
    }


class _Quiet:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log.debug('yt-dlp: %s', msg)

    def error(self, msg):
        log.info('yt-dlp: %s', msg)


def _extract(pid):
    import yt_dlp   # imported here: it is slow to load and only needed for YouTube links

    options = {'extract_flat': 'in_playlist', 'skip_download': True, 'quiet': True, 'no_warnings': True,
               'noprogress': True, 'logger': _Quiet(), 'playlistend': MAX_ITEMS, 'socket_timeout': 20}
    with yt_dlp.YoutubeDL(options) as ydl:
        try:
            return ydl.extract_info(f'https://www.youtube.com/playlist?list={pid}', download=False)
        except yt_dlp.utils.DownloadError as e:
            text = str(e)
            log.info('youtube playlist %s: %s', pid, text)
            if re.search(r'does not exist|private|unavailable|not found|404', text, re.I):
                raise UserError(NOT_FOUND, 404)
            raise UserError('Couldn\'t read that playlist from YouTube. Please try again in a minute.', 502)


@dataclass
class Video:
    id: str
    title: str      # the video's own title
    channel: str
    artist: str     # as split() reads them
    song: str


async def video(vid):
    """The song a single video is: its title and channel from YouTube's oEmbed answer,
    or from the watch page for a video whose owner turned embedding off."""
    url = WATCH_URL.format(id=vid)
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={'User-Agent': USER_AGENT}) as http:
            async with http.get(OEMBED_URL, params={'url': url, 'format': 'json'}) as res:
                if res.status == 200:
                    data = await res.json(content_type=None)
                    title, channel = data.get('title') or '', data.get('author_name') or ''
                elif res.status in (400, 404):
                    raise UserError(VIDEO_NOT_FOUND, 404)
                else:
                    # 401/403: embedding is off, but the watch page still says what it is
                    async with http.get(url, headers={'Accept-Language': 'en'}) as page:
                        text = await page.text()
                    t, c = META_TITLE.search(text), META_CHANNEL.search(text)
                    if not t or not t.group(1):
                        raise UserError(VIDEO_NOT_FOUND, 404)
                    title = html.unescape(t.group(1))
                    channel = json.loads(f'"{c.group(1)}"') if c else ''
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        log.warning('youtube video %s: %r', vid, e)
        raise UserError('Couldn\'t reach YouTube. Please try again in a minute.', 502)
    artist, song = split(title, channel)
    log.info('youtube video %s: %r by %r -> %r / %r', vid, title, channel, artist, song)
    return Video(vid, title, channel, artist, song)


class YouTube:
    name = 'YouTube'

    def __init__(self):
        self.reads = asyncio.Semaphore(READS)

    async def read(self, kind, xid):
        if kind == 'playlist':
            return await self.playlist(xid)
        found = await video(xid)
        return Song(found.song, found.artist, video=found.id,
                    extra={'title': found.title, 'channel': found.channel})

    async def playlist(self, pid):
        if pid.startswith('RD'):
            raise UserError(MIX)
        async with self.reads:
            try:
                info = await asyncio.wait_for(asyncio.to_thread(_extract, pid), TIMEOUT)
            except asyncio.TimeoutError:
                raise UserError('YouTube took too long to list that playlist. Please try again.', 504)
        if not info or info.get('_type') != 'playlist':
            raise UserError(NOT_FOUND, 404)
        entries = [e for e in info.get('entries') or [] if e]
        expected = info.get('playlist_count')
        if isinstance(expected, int) and len(entries) < min(expected, MAX_ITEMS):
            # A shorter CSV would quietly leave songs out, so refuse instead
            log.warning('youtube playlist %s: got %d of %d videos', pid, len(entries), expected)
            raise UserError('YouTube stopped partway through that playlist. Please try again.', 502)
        result = Playlist(pid, info.get('title') or 'YouTube playlist', info.get('channel') or info.get('uploader') or '')
        result.rows = [_row(e) for e in entries]
        result.unavailable = sum(not r['Track Name'] for r in result.rows)
        log.info('youtube playlist %s: %r, %d videos (%d unavailable)', pid, result.name, len(result.rows),
                 result.unavailable)
        return result
