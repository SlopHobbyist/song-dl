"""YouTube as a place to get a song that isn't on Soulseek.

A search asks YouTube for a handful of videos, scores them (an "Artist -
Topic" upload or an official audio beats a music video, which beats a fan's
upload; remixes, covers, loops and the like are dropped unless asked for), and
reads the best one's formats, so the result can say its quality and size.

The download is the best audio YouTube has, usually Opus at about 130 kbps,
sometimes AAC. It is never converted: ffmpeg only moves it out of its video
container (.webm -> .opus) and writes the artist and title into its tags. It
is saved in a UUID-named folder under the downloads folder, like a Soulseek
fetch, so the rest of Song-DL handles it the same way.

yt-dlp needs a JavaScript runtime (deno, in the Docker image) to read
YouTube's formats, and ffmpeg for the tags. YouTube changes often; if this
breaks, rebuild the image with --pull --no-cache to update yt-dlp.
"""

import asyncio
import logging
import math
import re
import shutil
import time
from pathlib import Path

from . import matching
from .errors import UserError
from .playlist import clean_title
from .youtube import WATCH_URL, _Quiet, split

log = logging.getLogger('songdl')

SEARCH_RESULTS = 10   # videos asked for per search
PROBES = 3            # best-scored videos read before giving up
KEPT = 4              # videos kept per song, to fall back on when one won't download
SEARCHES = 2          # YouTube searches (and probes) at once
DOWNLOADS = 3         # YouTube downloads at once
SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 15 * 60
MIN_SECONDS = 30
MAX_SECONDS = 20 * 60   # longer is a full album, a loop or a mix
LENGTH_CLOSE = 5        # seconds a video may differ from the song's length and count as the same recording
LENGTH_FAR = 30         # ...and past this it's another recording, when nobody said whose song it is
FORMAT = 'bestaudio/best'
VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
# A download that fails for one of these (a DNS or network hiccup) is tried again after each delay
NETWORK_TROUBLE = re.compile(
    r'failed to resolve|name or service not known|temporary failure in name resolution|timed out'
    r'|connection (?:reset|refused|aborted)|network is unreachable|remote end closed', re.I)
RETRY_DELAYS = (3, 10)

# The audio codecs yt-dlp's best audio comes in, and what Song-DL calls them (see matching.LABELS)
CODECS = {'opus': 'opus', 'mp4a': 'm4a', 'aac': 'm4a', 'vorbis': 'ogg', 'mp3': 'mp3'}
EXT_FORMATS = {'.opus': 'opus', '.m4a': 'm4a', '.ogg': 'ogg', '.mp3': 'mp3'}

# Videos that are about a song rather than the song
NOT_THE_SONG = {
    'loop', 'hour', 'hours', '1hour', '10hours', 'reaction', 'reacts', 'review', 'tutorial', 'lesson',
    'chords', 'tabs', 'trailer', 'teaser', 'snippet', 'preview', 'commercial', 'storyboard', 'interview',
    'documentary', 'shorts', 'parody', '8d', 'bassboosted', 'boosted', 'ringtone', 'mashup',
}
TOPIC = re.compile(r'\s+-\s+Topic$')
AUDIO = re.compile(r'\b(?:official\s+)?audio\b', re.I)
OFFICIAL = re.compile(r'\bofficial\b', re.I)
# YouTube's own uploads of a label's tracks. The search often shows their channel as just
# "Release - Topic", but the description starts "Provided to YouTube by <label> <title> · <artist> ·
# <artist> <album> ℗ ...", so the artists are in there
PROVIDED = re.compile(r'^Provided to YouTube by\b', re.I)
VIDEO_WORDS = re.compile(r'\b(?:music\s+video|official\s+video|video\s+oficial|clip\s+officiel|m/?v)\b', re.I)


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def _words(text):
    return set(matching.tokens(text or ''))


def credits(entry):
    """The label, title and artists from an auto-generated upload's description, or ''."""
    text = ' '.join((entry.get('description') or '').split())
    return text.split('℗')[0] if PROVIDED.match(text) else ''


def uploader_is(artists, channel):
    """Whether the channel or account is one of the artists' own: "Daft Punk", "Daft Punk -
    Topic", "DaftPunkVEVO"."""
    ch = _norm(matching.fold(TOPIC.sub('', channel)))
    for artist in artists:
        name = _norm(matching.fold(artist))
        if name and (ch in (name, name + 'vevo', name + 'official', name + 'music') or (len(name) >= 4 and name in ch)):
            return True
    return False


def score(entry, query, artist='', title='', duration=None, artists=(), exact=False):
    """How likely a search result is the song: a number, higher is better, or
    None when it isn't the song at all. `artist`, `title`, `duration` and
    `artists` (everyone credited) come from a playlist; a typed search only has
    the query. Also returns whether it is a longer version than the playlist's.

    For a playlist's song the upload has to name one of its artists (in its
    title, its uploader or YouTube's credits), in the version asked for, and
    be the playlist's length. Only an artist's own upload may be longer (an
    extended version, when `exact` doesn't rule that out), never shorter: a
    song of the same name by somebody else is the usual wrong pick, and it is
    almost never the same length."""
    name = ' '.join((entry.get('title') or '').split())
    channel = ' '.join((entry.get('channel') or entry.get('uploader') or '').split())
    length = entry.get('duration')
    if not name or not entry.get('id') or entry.get('live_status') in ('is_live', 'is_upcoming', 'post_live'):
        return None, False
    if not length or length < MIN_SECONDS or length > max(MAX_SECONDS, (duration or 0) * 1.5):
        return None, False

    credited = credits(entry)
    # An auto-generated upload says who the song is by: someone else's song of the same name isn't it
    who = _words(artist)
    if credited and who and len(who & _words(credited)) < math.ceil(len(who) / 2):
        return None, False
    text = _words(name) | _words(channel) | _words(credited)
    if TOPIC.search(channel):
        text |= _words(TOPIC.sub('', channel))
    # The song's title (all of a short one, most of a long one), and most of what was typed
    named = _words(clean_title(title)) if title else set()
    if named and len(named & _words(name)) < (len(named) if len(named) < 4 else math.ceil(len(named) * 0.8)):
        return None, False
    wanted = _words(query)
    everyone = tuple(a for a in (artists or (artist,)) if a)
    # (A remixer the title names is one of them, credited as an artist or not: "STRIPES Remix")
    remixer = bool(title and matching.key_words(channel) and set(matching.key_words(channel)) <= set(matching.words(title)))
    theirs = uploader_is(everyone, channel) or remixer
    if everyone:
        said = set(matching.words(name)) | set(matching.words(channel)) | set(matching.words(credited))
        if not theirs and not matching.artist_share(everyone, said):
            return None, False
        # Whose it is has been settled, by any of the artists (a remix is often on its remixer's
        # account): what was typed only has to cover the rest
        wanted = (wanted - _words(artist)) or wanted
    if wanted and len(wanted & text) < max(1, math.ceil(len(wanted) * 0.75)):
        return None, False
    # "Dosem Mix" isn't the "Victor Simonelli Club Mix": every word that says which version
    if title and not matching.title_parts(title)[1] <= set(matching.words(name)):
        return None, False
    # A remix, live take, cover... is only the song if it was asked for
    asked = wanted | _words(title)
    if ((_words(name) & (matching.VARIANT_WORDS | NOT_THE_SONG)) - asked
            and not matching.NEUTRAL_PHRASES.search(name.lower())):
        return None, False

    points = 0.0
    topic = bool(TOPIC.search(channel))
    if topic:
        points += 4          # YouTube Music's own upload of the album track
    elif AUDIO.search(name) and not VIDEO_WORDS.search(name):
        points += 3
    elif VIDEO_WORDS.search(name):
        points -= 1          # a music video often has an intro or a skit
    if OFFICIAL.search(name):
        points += 1
    who = _norm(artist) or None
    if who and (who in _norm(channel) or _norm(TOPIC.sub('', channel)) in (who, who + 'vevo')
                or who in _norm(credited)):
        points += 2
    elif not who and (topic or channel.endswith('VEVO')):
        points += 1
    outlier = False
    if duration:
        off = abs(length - duration)
        if off <= LENGTH_CLOSE:
            points += 3
        elif everyone:
            if off > matching.LENGTH_TOLERANCE:
                if not theirs or matching.length_fit(round(length), duration, exact) != 'longer':
                    return None, False
                outlier = True
        elif off > LENGTH_FAR:
            points -= 3
            outlier = True
    points += math.log10((entry.get('view_count') or 0) + 1) / 3
    return points, outlier


def candidate(entry, meta, outlier=False):
    """A matching.Candidate for a video. Its quality and size are filled in by probing it."""
    vid = entry['id']
    c = matching.Candidate(
        username=entry.get('channel') or entry.get('uploader') or 'YouTube',
        filename=f'{entry.get("title") or vid}.opus',
        size=0, length=round(entry['duration']) if entry.get('duration') else None,
        bit_depth=None, sample_rate=None, free_slot=True, queue=0, speed=0, fmt='opus',
        source='youtube', url=WATCH_URL.format(id=vid))
    c.outlier = outlier
    c.fit = 'longer' if outlier else ''
    c.meta = dict(meta)
    return c


def _apply_format(cand, info):
    """Fills in a candidate from a video's full info, with the best audio format chosen."""
    fmt = CODECS.get((info.get('acodec') or '').split('.')[0].lower(), 'm4a' if info.get('ext') == 'm4a' else 'opus')
    cand.fmt = fmt
    cand.filename = f'{info.get("title") or info.get("id")}.{fmt}'
    cand.bitrate = round(info['abr']) if info.get('abr') else None
    cand.sample_rate = info.get('asr')
    cand.size = int(info.get('filesize') or info.get('filesize_approx') or 0)
    if info.get('duration'):
        cand.length = round(info['duration'])
    cand.username = info.get('channel') or info.get('uploader') or cand.username
    return cand


def _options(cfg, **extra):
    options = {'quiet': True, 'no_warnings': True, 'noprogress': True, 'logger': _Quiet(), 'noplaylist': True,
               'socket_timeout': 20, 'format': FORMAT, 'retries': 3, 'extractor_retries': 2}
    if cfg is not None:
        options['cachedir'] = str(cfg.data_dir / 'yt-dlp-cache')
    options.update(extra)
    return options


def _search(cfg, query):
    import yt_dlp

    with yt_dlp.YoutubeDL(_options(cfg, extract_flat='in_playlist', skip_download=True)) as ydl:
        info = ydl.extract_info(f'ytsearch{SEARCH_RESULTS}:{query}', download=False)
    return [e for e in (info or {}).get('entries') or [] if e]


def _probe(cfg, url):
    import yt_dlp

    with yt_dlp.YoutubeDL(_options(cfg, skip_download=True)) as ydl:
        return ydl.extract_info(url, download=False)


class Cancelled(Exception):
    pass


def _download(cfg, cand, folder: Path, fetch):
    """Saves the video's (or SoundCloud track's) best audio into the folder and returns its path. Reports
    progress on `fetch` (bytes, total, speed, phase) and stops when fetch.cancelled is set."""
    import yt_dlp

    def progress(d):
        if fetch.cancelled:
            raise Cancelled()
        if d.get('status') == 'downloading':
            fetch.phase = 'transferring'
            fetch.bytes = d.get('downloaded_bytes') or 0
            fetch.total = d.get('total_bytes') or d.get('total_bytes_estimate') or fetch.total
            fetch.speed = round(d.get('speed') or 0)

    options = _options(
        cfg, outtmpl=str(folder / '%(id)s.%(ext)s'), progress_hooks=[progress], overwrites=True,
        postprocessors=[{'key': 'FFmpegExtractAudio', 'preferredcodec': 'best'},
                        {'key': 'FFmpegMetadata', 'add_metadata': True}])
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for delay in (*RETRY_DELAYS, None):
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(cand.url, download=False, process=False)
                    if fetch.cancelled:
                        raise Cancelled()
                    # Our artist and title, not YouTube's "Artist - Song (Official Video)"
                    for key in ('title', 'artist', 'album'):
                        if cand.meta.get(key):
                            info['meta_' + key] = cand.meta[key]
                    info = ydl.process_ie_result(info, download=True)
                break
            except Exception as e:
                if delay is None or fetch.cancelled or isinstance(e, Cancelled) or not NETWORK_TROUBLE.search(str(e)):
                    raise
                log.info('network trouble downloading %s, trying again in %ds: %s', cand.url, delay, e)
                time.sleep(delay)
        paths = [Path(d['filepath']) for d in (info or {}).get('requested_downloads') or [] if d.get('filepath')]
        path = next((p for p in paths if p.is_file()), None)
        if not path:
            raise UserError('Nothing came back to save')
        return path
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def passing(e):
    """The status for a UserError about a failed search: 503 when the network let it down (a DNS
    hiccup, say), so it's worth trying again in a while; 502 for anything else."""
    return 503 if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or NETWORK_TROUBLE.search(str(e)) else 502


def download_error(e):
    """What to tell a person about a failed YouTube download."""
    text = str(e)
    if NETWORK_TROUBLE.search(text):
        return 'The server couldn\'t reach YouTube'
    if re.search(r'sign in to confirm|not a bot', text, re.I):
        return 'YouTube is asking Song-DL to prove it isn\'t a bot. Please try again later.'
    if re.search(r'confirm your age|age[- ]restricted|inappropriate', text, re.I):
        return 'YouTube only shows that video to signed-in adults'
    if re.search(r'private|unavailable|removed|terminated|copyright|blocked', text, re.I):
        return 'That video isn\'t available on YouTube any more'
    if re.search(r'ffmpeg|ffprobe', text, re.I):
        return 'The server is missing ffmpeg, which YouTube downloads need'
    return 'YouTube didn\'t send the audio'


class YouTubeAudio:
    source = 'youtube'

    def __init__(self, cfg):
        self.cfg = cfg
        self.searches = asyncio.Semaphore(SEARCHES)
        self.downloads = asyncio.Semaphore(DOWNLOADS)

    async def _run(self, func, *args, timeout=SEARCH_TIMEOUT):
        return await asyncio.wait_for(asyncio.to_thread(func, *args), timeout)

    async def _search(self, query):
        try:
            return await self._run(_search, self.cfg, query)
        except Exception as e:   # yt-dlp's DownloadError, or a timeout
            log.info('youtube search %r failed: %r', query, e)
            raise UserError('Couldn\'t search YouTube. Please try again in a minute.', passing(e))

    @staticmethod
    def _score(entries, query, artist, title, duration, artists=(), exact=False):
        """[(points, outlier, entry)] of the entries that might be the song."""
        scored = []
        for entry in entries:
            points, outlier = score(entry, query, artist, title, duration, artists, exact)
            if points is not None:
                scored.append((points, outlier, entry))
        return scored

    async def find(self, query, artist='', title='', duration=None, video=None, more=False, artists=(), exact=False):
        """The videos that are the song, best first, the first with its quality
        read; [] when YouTube has none. `video` is a video ID to take as is."""
        async with self.searches:
            if video:
                if not VIDEO_ID.match(video):
                    return []
                picks = [({'id': video, 'title': '', 'duration': None}, False)]
            else:
                entries = await self._search(query)
                scored = self._score(entries, query, artist, title, duration, artists, exact)
                if not any(not outlier for _, outlier, _ in scored):
                    # Nothing the right length. YouTube ranks a song's own auto-generated upload
                    # ("Artist - Topic", "Provided to YouTube by...") low for some songs, but
                    # finds it when asked for one
                    seen = {e.get('id') for e in entries}
                    extra = [e for e in await self._search(query + ' topic') if e.get('id') not in seen]
                    entries += extra
                    scored += self._score(extra, query, artist, title, duration, artists, exact)
                scored.sort(key=lambda s: (not s[1], s[0]), reverse=True)
                picks = [(entry, outlier) for _, outlier, entry in scored[:KEPT + PROBES]]
                log.info('youtube search %r: %d of %d videos might be the song', query, len(scored), len(entries))

            # Read the best one's formats; a video that can't be read (age-restricted, say) is skipped
            trouble = None
            for i, (entry, outlier) in enumerate(picks[:PROBES]):
                url = WATCH_URL.format(id=entry['id'])
                try:
                    info = await self._run(_probe, self.cfg, url)
                except Exception as e:
                    log.info('youtube video %s can\'t be read: %r', entry['id'], e)
                    if passing(e) == 503:
                        trouble = e
                    continue
                if not info or not info.get('duration'):
                    continue
                a, s = split(info.get('title'), info.get('channel') or info.get('uploader'))
                meta = {'title': title or s, 'artist': artist or a, 'album': ''}
                best = _apply_format(candidate({**entry, 'duration': info['duration']}, meta, outlier), info)
                rest = [candidate(e, meta, o) for e, o in picks[i + 1:i + KEPT] if e.get('duration')]
                for c in rest:
                    c.fmt = best.fmt
                    c.filename = re.sub(r'\.\w+$', '.' + best.fmt, c.filename)
                return [best] + rest
            if trouble:
                # Not "YouTube doesn't have it": YouTube couldn't be reached, so it's worth
                # asking again later
                raise UserError('Couldn\'t reach YouTube. Please try again in a minute.', 503)
            return []

    async def download(self, cand, folder, fetch):
        """Fetches the candidate's audio into the folder; see _download."""
        fetch.phase = 'waiting'
        async with self.downloads:
            fetch.phase = 'requesting'
            return await self._run(_download, self.cfg, cand, folder, fetch, timeout=DOWNLOAD_TIMEOUT)


def format_of(path: Path):
    """The format (a matching.LABELS key) a downloaded file is, by its extension."""
    return EXT_FORMATS.get(path.suffix.lower())
