"""SoundCloud as a place to get a song that isn't on Soulseek, tried before YouTube.

One search asks SoundCloud's API for a page of tracks. Each track says how
it streams, so tracks that can't be downloaded are dropped before scoring
rather than found out one by one:
- tracks under DRM, which is most major-label uploads;
- 30-second previews of Go+ tracks;
- tracks blocked in the server's country.

The rest are scored like YouTube videos (see youtube_audio.score), along
with SoundCloud's own edits, flips and the like. Each one's quality comes
from its stream list, so nothing needs probing.

The download is the best stream SoundCloud gives anyone, usually AAC at
160 kbps (sometimes 96), never converted, with the artist and title
written into its tags. The uploader's original file, which can be lossless,
needs a signed-in SoundCloud account, so it isn't used.

SoundCloud's API needs a client ID, which yt-dlp reads from SoundCloud's
website. The search borrows yt-dlp's SoundCloud extractor for that. If a
yt-dlp update changes the extractor, it falls back to yt-dlp's plain search,
which can't tell which tracks are under DRM.
"""

import asyncio
import logging
import math
import re

from . import matching, youtube_audio
from .errors import UserError
from .youtube import split
from .youtube_audio import Cancelled, _download, _options, format_of  # noqa: F401 (Cancelled, format_of: the same as YouTube's)

log = logging.getLogger('songdl')

SEARCH_RESULTS = 20   # tracks asked for per search; many are someone's edit
MIN_KBPS = 128        # lower (AAC 96k) is worse than YouTube's, so it's only taken when YouTube isn't next
KEPT = 4              # tracks kept per song, to fall back on when one won't download
SEARCHES = 2          # SoundCloud searches at once
DOWNLOADS = 3         # SoundCloud downloads at once
SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 15 * 60
TRACK_URL = re.compile(r'^https://(?:www\.|m\.)?soundcloud\.com/[\w-]+/[\w-]+/?$')

# A stream's preset ("aac_160k", "mp3_1_0", "opus_0_0") -> the format and its kbps
PRESET_FORMATS = {'aac': 'm4a', 'mp3': 'mp3', 'opus': 'opus'}
PRESET_KBPS = {'mp3': 128, 'opus': 64, 'aac': 160}

# What SoundCloud's producers call their own takes on a song (on top of matching.VARIANT_WORDS)
NOT_THE_SONG = {'flip', 'refix', 'reedit', 'blend', 'redo', 'relove', 'rerub', 'rerubs', 'jersey', 'reboot',
                'private', 'daycore', 'version', 'versions', 'edition', 'retake', 'screwed', 'chopped',
                'unreleased', 'ver', 'vaporwave', 'lofi'}
# ...and what accounts that only post sped-up or slowed-down songs call themselves
ALTERED = {'slowed', 'sped', 'reverb', 'nightcore', 'daycore', '8d', 'bassboosted'}
# "Song A x Song B", "Song + Other Song", "Flood vs Delilah": two songs at once
MASHUP = re.compile(r"\s(?:x|vs\.?|\+)\s", re.I)
DASH = re.compile(r'\s[-–—]\s')
BRACKETS = re.compile(r'\s*[(\[【][^)\]】]*[)\]】]')
FREE_DOWNLOAD = re.compile(r'\s*[\[(【]?\s*\bfree\s*(?:download|dl)\b\s*[\])】]?', re.I)


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _words(text):
    return set(matching.tokens(text or ''))


def _clean(title):
    return ' '.join(FREE_DOWNLOAD.sub(' ', title or '').split()).strip(' -–—|')


def _stream(item):
    """(format, kbps) of the best stream anyone may play, or None when there isn't one
    (DRM, a Go+ preview). Leaves out the "hq" streams only Go+ subscribers get."""
    if item.get('policy') == 'BLOCK' or item.get('streamable') is False:
        return None
    best = None
    for t in (item.get('media') or {}).get('transcodings') or []:
        preset = t.get('preset') or ''
        protocol = ((t.get('format') or {}).get('protocol')) or ''
        if (protocol.startswith(('ctr-', 'cbc-')) or preset.startswith('abr') or t.get('snipped')
                or '/preview/' in (t.get('url') or '') or t.get('quality') == 'hq'):
            continue
        codec = preset.split('_')[0]
        fmt = PRESET_FORMATS.get(codec)
        if not fmt:
            continue
        m = re.search(r'(\d+)k$', preset)
        kbps = int(m.group(1)) if m else PRESET_KBPS[codec]
        if not best or kbps > best[1]:
            best = (fmt, kbps)
    return best


def _track(item):
    """One of the API's tracks, as the few things Song-DL needs from it."""
    credited = (item.get('publisher_metadata') or {}).get('artist') or ''
    return {
        'id': str(item.get('id') or ''),
        'title': _clean(item.get('title')),
        'user': ((item.get('user') or {}).get('username')) or '',
        'credited': credited,
        'duration': (item.get('duration') or 0) / 1000,
        'full': (item.get('full_duration') or item.get('duration') or 0) / 1000,   # a Go+ preview's is 30 s
        'plays': item.get('playback_count') or 0,
        'url': item.get('permalink_url') or '',
        'stream': _stream(item),
    }


def _flat_track(entry):
    """The same, from yt-dlp's plain search: it can't say how a track streams, so it's assumed fine."""
    return {
        'id': str(entry.get('id') or ''),
        'title': _clean(entry.get('title')),
        'user': entry.get('uploader') or '',
        'credited': ', '.join(entry.get('artists') or []),
        'duration': entry.get('duration') or 0,
        'full': entry.get('duration') or 0,
        'plays': entry.get('view_count') or 0,
        'url': entry.get('webpage_url') or '',
        'stream': ('m4a', None),
    }


def score(track, query, artist='', title='', duration=None, locked=False, min_kbps=0):
    """How likely a track is the song (see youtube_audio.score): a number or None, and whether
    its length disagrees with the playlist's. A track nobody may download scores None,
    unless `locked` asks how well it would have."""
    if not (track['stream'] or locked) or not TRACK_URL.match(track['url']):
        return None, False
    if track['stream'] and track['stream'][1] and track['stream'][1] < min_kbps:
        return None, False
    channel = track['user']
    if track['credited'] and _norm(track['credited']) not in _norm(channel):
        channel = f'{channel} {track["credited"]}'   # who SoundCloud says it's by counts as much as who posted it
    entry = {'id': track['id'], 'title': track['title'], 'channel': channel, 'duration': track['duration'],
             'view_count': track['plays']}
    points, outlier = youtube_audio.score(entry, query, artist, title, duration)
    if points is None:
        return None, False
    asked = _words(query) | _words(title)
    name = track['title']
    if ((_words(name) & NOT_THE_SONG) - asked and not matching.NEUTRAL_PHRASES.search(name.lower())
            or (_words(track['user']) & ALTERED) - asked):
        return None, False
    # The song's part of "Artist x Artist - Song", without its brackets
    # ...is two songs at once unless every one of them was asked for
    song = BRACKETS.sub('', DASH.split(name, maxsplit=1)[-1])
    parts = [_words(p) for p in MASHUP.split(song)]
    if len(parts) > 1 and any(len(p & asked) < max(1, math.ceil(len(p) * 0.75)) for p in parts):
        return None, False
    if track['stream'] and (track['stream'][1] or 0) >= 160:
        points += 0.5
    return points, outlier


def candidate(track, meta, outlier=False):
    fmt, kbps = track['stream']
    c = matching.Candidate(
        username=track['user'] or 'SoundCloud',
        filename=f'{track["title"] or track["id"]}.{fmt}',
        size=round(kbps * 1000 / 8 * track['duration']) if kbps else 0,
        length=round(track['duration']) or None,
        bit_depth=None, sample_rate=None, free_slot=True, queue=0, speed=0, fmt=fmt,
        bitrate=kbps, source='soundcloud', url=track['url'])
    c.outlier = outlier
    c.meta = dict(meta)
    return c


def _search(cfg, query):
    """The tracks SoundCloud finds for the query (see _track)."""
    import yt_dlp
    from yt_dlp.utils import update_url_query

    with yt_dlp.YoutubeDL(_options(cfg, extract_flat='in_playlist', skip_download=True)) as ydl:
        try:
            ie = ydl.get_info_extractor('SoundcloudSearch')
            ie.initialize()
            url = update_url_query(ie._API_V2_BASE + 'search/tracks', {'q': query, 'limit': SEARCH_RESULTS})
            items = ie._call_api(url, query, 'Searching', headers=ie._HEADERS).get('collection') or []
        except (AttributeError, TypeError, KeyError) as e:
            # yt-dlp's SoundCloud extractor has changed under us
            log.warning('soundcloud: reading the API directly failed (%r), using yt-dlp\'s search', e)
            info = ydl.extract_info(f'scsearch{SEARCH_RESULTS}:{query}', download=False)
            return [_flat_track(e) for e in (info or {}).get('entries') or [] if e]
    return [_track(i) for i in items if isinstance(i, dict) and i.get('kind', 'track') == 'track']


def download_error(e):
    """What to tell a person about a failed SoundCloud download."""
    text = str(e)
    if youtube_audio.NETWORK_TROUBLE.search(text):
        return 'The server couldn\'t reach SoundCloud'
    if re.search(r'DRM', text):
        return 'SoundCloud only lets its app play that track'
    if re.search(r'geo|not available (?:in|from) your', text, re.I):
        return 'SoundCloud doesn\'t play that track in the server\'s country'
    if re.search(r'429|rate limit', text, re.I):
        return 'SoundCloud is getting too many requests from Song-DL. Please try again in a few minutes.'
    if re.search(r'404|not found|private|removed|unavailable', text, re.I):
        return 'That track isn\'t on SoundCloud any more'
    if re.search(r'ffmpeg|ffprobe', text, re.I):
        return 'The server is missing ffmpeg, which SoundCloud downloads need'
    return 'SoundCloud didn\'t send the audio'


class SoundCloudAudio:
    source = 'soundcloud'

    def __init__(self, cfg):
        self.cfg = cfg
        self.searches = asyncio.Semaphore(SEARCHES)
        self.downloads = asyncio.Semaphore(DOWNLOADS)

    async def _run(self, func, *args, timeout=SEARCH_TIMEOUT):
        return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout)

    async def find(self, query, artist='', title='', duration=None, video=None, more=False):
        """The tracks that are the song, best first; [] when SoundCloud has none. `more`
        says another place (YouTube) will be tried next, so a poor copy is left to it."""
        async with self.searches:
            try:
                tracks = await self._run(_search, self.cfg, query)
            except Exception as e:   # yt-dlp's DownloadError, or a timeout
                log.info('soundcloud search %r failed: %r', query, e)
                raise UserError('Couldn\'t search SoundCloud. Please try again in a minute.', 502)
        if not duration:
            # The label's own upload is usually under DRM, but it still says how long the song is
            locked = [(points, t['full']) for t in tracks if not t['stream'] and t['full']
                      for points in [score({**t, 'duration': t['full']}, query, artist, title, locked=True)[0]]
                      if points is not None]
            if locked:
                duration = max(locked)[1]
        scored = []
        for t in tracks:
            points, outlier = score(t, query, artist, title, duration, min_kbps=MIN_KBPS if more else 0)
            # So many SoundCloud uploads are someone's edit that one the wrong length is left to YouTube
            if points is not None and not outlier:
                scored.append((points, outlier, t))
        scored.sort(key=lambda s: (not s[1], s[0]), reverse=True)
        log.info('soundcloud search %r: %d of %d tracks might be the song', query, len(scored), len(tracks))
        if not scored:
            return []
        best = scored[0][2]
        a, s = split(best['title'], best['credited'] or best['user'])
        meta = {'title': title or s, 'artist': artist or a, 'album': ''}
        return [candidate(t, meta, outlier) for _, outlier, t in scored[:KEPT]]

    async def download(self, cand, folder, fetch):
        """Fetches the track's audio into the folder; see youtube_audio._download."""
        fetch.phase = 'waiting'
        async with self.downloads:
            fetch.phase = 'requesting'
            return await self._run(_download, self.cfg, cand, folder, fetch, timeout=DOWNLOAD_TIMEOUT)
