"""Reads Amazon Music playlist, album and song links.

music.amazon.com's web player gets everything from Amazon's "mesk" API
({na,eu,fe}.mesk.skill.music.a2z.com) with the device and session ids that
config.json hands any visitor, signed in or not. A playlist or album comes
back whole, in one answer shaped like the page it draws (a "DetailTemplate"
whose table widget lists the songs); it is big, and Amazon refuses to send it
unless asked for gzip.

Each link is read on its own country's site (music.amazon.co.uk and so on),
since a playlist from one country may not exist in another.

Not read: links to /tracks/<id> (only search engines' and social sites'
link-preview bots are told which album such a song is on; Amazon's Share
button gives an /albums/<id>?trackAsin=<id> link instead, which works), and the
names of the people who made user playlists, for the same reason.
"""

import asyncio
import json
import logging
import re
import time
import uuid

import aiohttp

from .catalog import USER_AGENT, Playlist, ServiceError, Song, row
from .errors import UserError

log = logging.getLogger('songdl')

MESK = 'https://{region}.mesk.skill.music.a2z.com/api/'
REGIONS = {'NA': 'na', 'EU': 'eu', 'FE': 'fe'}
SITE = 'https://{domain}'
CONFIG_LIFE = 6 * 3600
TIMEOUT = 60          # a playlist of a thousand songs takes several seconds
RETRIES = 4

# music.amazon.<tld>/<kind>/<id>, or (www.)amazon.<tld>/music/player/<kind>/<id>
LINK = re.compile(
    r'\b(?:(music)\.amazon\.|(?:www\.)?amazon\.)'
    r'(?P<tld>com\.(?:au|br|mx)|co\.(?:uk|jp)|com|de|fr|it|es|ca|in)'
    r'(?(1)|/music/player)'
    r'/(?P<kind>playlists|user-playlists|albums|tracks)/(?P<id>[A-Za-z0-9]+)(?P<rest>[^\s#]*)', re.I)
ASIN = re.compile(r'^B[0-9A-Z]{9}$')
USER_PLAYLIST = re.compile(r'^[0-9a-f]{32}[a-z0-9]{4}$')
TRACK_PARAM = re.compile(r'[?&]trackAsin=([0-9A-Z]{10})\b', re.I)
COUNT = re.compile(r'\s*([\d,]+) SONGS?\b', re.I)
UNSUPPORTED = [(re.compile(r'\b(?:music\.amazon\.[a-z.]+|amazon\.[a-z.]+/music/player)/tracks/', re.I),
                'Song-DL can\'t read Amazon Music links to /tracks/. In Amazon Music, use the song\'s '
                'Share button and paste that link (it has "trackAsin" in it).')]

NOT_FOUND = {
    'playlist': 'Amazon Music couldn\'t find that playlist. Check the link, and that the playlist is public.',
    'album': 'Amazon Music couldn\'t find that album. Check the link.',
    'song': 'Amazon Music couldn\'t find that song. Check the link.',
}


def find(text):
    """(kind, "<domain>/<id>") for an Amazon Music link, or None. A song's id is
    "<album>?<track>", since a song is read from its album."""
    m = LINK.search(str(text or ''))
    if not m:
        return None
    domain = 'music.amazon.' + m.group('tld').lower()
    kind, ident = m.group('kind').lower(), m.group('id')
    if kind == 'user-playlists':
        return ('playlist', f'{domain}/user:{ident.lower()}') if USER_PLAYLIST.match(ident.lower()) else None
    if not ASIN.match(ident.upper()) or kind == 'tracks':
        return None
    ident = ident.upper()
    if kind == 'playlists':
        return 'playlist', f'{domain}/{ident}'
    if t := TRACK_PARAM.search(m.group('rest')):
        return 'song', f'{domain}/{ident}?{t.group(1).upper()}'
    return 'album', f'{domain}/{ident}'


def _seconds(text):
    """'03:29' or '1:02:03' -> seconds."""
    total = 0
    for part in re.findall(r'\d+', text or '')[-3:]:
        total = total * 60 + int(part)
    return total or None


# Amazon's titles say "[Explicit]", which no file on Soulseek is named
EXPLICIT = re.compile(r'\s*\[(?:Explicit|Clean)\]', re.I)


def _text(value):
    return EXPLICIT.sub('', (value.get('text') if isinstance(value, dict) else value) or '')


class AmazonMusic:
    name = 'Amazon Music'

    def __init__(self):
        self.configs = {}       # domain -> (time read, config.json)
        self.lock = asyncio.Lock()

    async def read(self, kind, ref):
        domain, ident = ref.split('/', 1)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT),
                                         headers={'User-Agent': USER_AGENT}) as http:
            if kind == 'song':
                album, track = ident.split('?', 1)
                return await self._song(http, domain, album, track)
            if ident.startswith('user:'):
                template = await self._template(http, domain, 'showLibraryPlaylist', ident[5:], kind)
            else:
                template = await self._template(http, domain,
                                                'showCatalogPlaylist' if kind == 'playlist' else 'showCatalogAlbum',
                                                ident, kind)
        return self._list(kind, ident, template)

    async def _config(self, http, domain):
        async with self.lock:
            got = self.configs.get(domain)
            if got and time.time() - got[0] < CONFIG_LIFE:
                return got[1]
            async with http.post(SITE.format(domain=domain) + '/config.json',
                                 params={'skipToken': 'false', 'clientApplication': 'skyfire'}) as res:
                try:
                    config = await res.json(content_type=None)
                except ValueError:
                    config = None
            if not isinstance(config, dict) or not config.get('deviceId'):
                raise ServiceError(f'{domain}/config.json: no deviceId')
            self.configs[domain] = (time.time(), config)
            return config

    async def _template(self, http, domain, endpoint, ident, kind):
        """The page Amazon describes for a playlist or album: its DetailTemplate."""
        for attempt in range(RETRIES):
            config = await self._config(http, domain)
            page = SITE.format(domain=domain) + '/'
            csrf = config.get('csrf') or {}
            headers = {
                'x-amzn-authentication': json.dumps({
                    'interface': 'ClientAuthenticationInterface.v1_0.ClientTokenElement', 'accessToken': ''}),
                'x-amzn-device-model': 'WEBPLAYER', 'x-amzn-device-width': '1920', 'x-amzn-device-height': '1080',
                'x-amzn-device-family': 'WebPlayer', 'x-amzn-device-id': config['deviceId'],
                'x-amzn-user-agent': USER_AGENT, 'x-amzn-session-id': config.get('sessionId') or '',
                'x-amzn-request-id': str(uuid.uuid4()), 'x-amzn-device-language': 'en_US',
                'x-amzn-currency-of-preference': 'USD', 'x-amzn-os-version': '1.0',
                'x-amzn-application-version': config.get('version') or '',
                'x-amzn-device-time-zone': 'America/New_York', 'x-amzn-timestamp': str(int(time.time() * 1000)),
                'x-amzn-csrf': json.dumps({'interface': 'CSRFInterface.v1_0.CSRFHeaderElement',
                                           'token': csrf.get('token', ''), 'timestamp': csrf.get('ts', ''),
                                           'rndNonce': csrf.get('rnd', '')}),
                'x-amzn-music-domain': domain, 'x-amzn-referer': '', 'x-amzn-affiliate-tags': '',
                'x-amzn-ref-marker': '', 'x-amzn-page-url': page, 'x-amzn-weblab-id-overrides': '',
                'x-amzn-video-player-token': '', 'x-amzn-feature-flags': 'hd-supported,uhd-supported',
                'x-amzn-has-profile-id': '', 'x-amzn-age-band': '',
            }
            body = {'id': ident, 'userHash': json.dumps({'level': 'LIBRARY_MEMBER'}), 'headers': json.dumps(headers)}
            url = MESK.format(region=REGIONS.get(config.get('siteRegion'), 'na')) + endpoint
            async with http.post(url, data=json.dumps(body), headers={
                    'Content-Type': 'text/plain;charset=UTF-8', 'Accept-Encoding': 'gzip',
                    'Origin': page.rstrip('/'), 'Referer': page}) as res:
                status = res.status
                raw = await res.text()
            if status == 429 or status >= 500:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if status in (400, 401, 403) and attempt == 0:
                self.configs.pop(domain, None)   # a stale session: read config.json again
                continue
            if status != 200:
                raise ServiceError(f'{endpoint} answered {status}: {raw[:200]}')
            try:
                answer = json.loads(raw)
            except ValueError:
                raise ServiceError(f'{endpoint}: not JSON')
            for method in answer.get('methods') or []:
                template = (method or {}).get('template') or {}
                if str(template.get('interface', '')).endswith('DetailTemplate'):
                    return template
            # "We're sorry, but this playlist is no longer available.", or a "Service error"
            # dialog for an id that is no album at all
            log.info('amazon %s %s: no page: %s', endpoint, ident, raw[:300])
            raise UserError(NOT_FOUND[kind], 404)
        raise ServiceError(f'{endpoint}: gave up after {RETRIES} tries')

    @staticmethod
    def _items(template):
        items = []
        for widget in template.get('widgets') or []:
            if str((widget or {}).get('interface', '')).endswith(('VisualTableWidgetElement',
                                                                  'DescriptiveTableWidgetElement')):
                if widget.get('onEndOfWidget'):
                    # Never seen: Amazon has always sent the whole list at once
                    raise ServiceError('the list has more pages')
                items += [i for i in widget.get('items') or [] if isinstance(i, dict)]
        return items

    @staticmethod
    def _track_asin(item):
        for key in ('primaryLink', 'primaryTextLink'):
            link = (item.get(key) or {}).get('deeplink') or ''
            if m := TRACK_PARAM.search(link) or re.search(r'/tracks/([0-9A-Z]{10})', link):
                return m.group(1)
        return None

    def _songs(self, kind, template):
        """(title, artist, album, seconds, asin) for each song in a playlist's or album's page."""
        name, artist = _text(template.get('headerText')), _text(template.get('headerPrimaryText'))
        for item in self._items(template):
            if kind == 'album':
                # An album's songs name their artist only where it isn't the album's
                yield (_text(item.get('primaryText')), _text(item.get('secondaryText2')) or artist, name,
                       _seconds(_text(item.get('secondaryText3'))), self._track_asin(item))
            else:
                yield (_text(item.get('primaryText')), _text(item.get('secondaryText1')),
                       _text(item.get('secondaryText2')), _seconds(_text(item.get('secondaryText3'))),
                       self._track_asin(item))

    def _list(self, kind, ident, template):
        name = _text(template.get('headerText')) or f'Amazon Music {kind}'
        owner = re.sub(r'^Curated by\s+', '', _text(template.get('headerPrimaryText')))
        if ident.startswith('user:'):
            owner = ''    # it says when the playlist was made, not who made it
        songs = list(self._songs(kind, template))
        rows = [row(title, [artist], album, (secs or 0) * 1000) for title, artist, album, secs, _ in songs if title]
        m = COUNT.match(_text(template.get('headerTertiaryText')))
        if m and len(songs) < int(m.group(1).replace(',', '')):
            raise ServiceError(f'{ident}: got {len(songs)} of {m.group(1)} songs')
        log.info('amazon %s %s: %r, %d songs', kind, ident, name, len(rows))
        return Playlist(ident.removeprefix('user:'), name, owner, rows)

    async def _song(self, http, domain, album, track):
        template = await self._template(http, domain, 'showCatalogAlbum', album, 'song')
        for title, artist, album_name, secs, asin in self._songs('album', template):
            if asin and asin.upper() == track.upper():
                return Song(title, artist, album_name, secs)
        raise UserError(NOT_FOUND['song'], 404)

