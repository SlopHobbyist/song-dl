"""Reading links from Apple Music, Deezer, Tidal, Qobuz, Pandora and Beatport
(links.py and a module per service), against one fake that answers like each."""

import base64
import json
import time

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from songdl import amazon, apple, beatport, deezer, links, pandora, playlist, qobuz, tidal
from songdl.errors import UserError

from tests.test_site import scan


def _jwt(claims):
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')  # noqa: E731
    return f'{part({"alg": "ES256", "kid": "WebPlayKid"})}.{part(claims)}.c2ln'


def song(i):
    return {'title': f'Song {i}', 'artist': 'Earth, Wind & Fire' if i == 0 else f'Artist {i}',
            'album': f'Album {i}', 'seconds': 200 + i, 'isrc': f'US0000000{i:03}'}


class FakeServices:
    def __init__(self):
        self.count = 250            # songs on every playlist
        self.apple_token = _jwt({'iss': 'AMPWebPlay', 'exp': time.time() + 86400 * 60})
        self.tidal_client = tidal.CLIENT[0]
        self.qobuz_app = qobuz.APP_ID
        self.base = None
        self.calls = []

    def songs(self, n=None):
        return [song(i) for i in range(self.count if n is None else n)]

    def app(self):
        r = web.RouteTableDef()

        # Deezer --------------------------------------------------------
        @r.get('/deezer/{kind}/{id}')
        async def dz_info(request):
            kind, xid = request.match_info['kind'], request.match_info['id']
            if kind == 'track':
                if xid != '5':
                    return web.json_response({'error': {'type': 'DataException', 'code': 800}})
                return web.json_response({'title': 'Digital Love', 'artist': {'name': 'Daft Punk'},
                                          'contributors': [{'name': 'Daft Punk', 'role': 'Main'}],
                                          'album': {'title': 'Discovery'}, 'duration': 301})
            if xid != '42':
                return web.json_response({'error': {'type': 'DataException', 'message': 'no data', 'code': 800}})
            # One song is gone from Deezer: counted, but left out of the pages
            return web.json_response({'title': 'Deezer Mix', 'creator': {'name': 'dz'}, 'artist': {'name': 'Band'},
                                      'nb_tracks': self.count + 1})

        @r.get('/deezer/{kind}/{id}/tracks')
        async def dz_tracks(request):
            index, size = int(request.query['index']), int(request.query['limit'])
            self.calls.append(('deezer', index))
            items = [{'title': s['title'], 'artist': {'name': s['artist']}, 'album': {'title': s['album']},
                      'duration': s['seconds'], 'isrc': s['isrc']} for s in self.songs()[index:index + size]]
            body = {'data': items, 'total': self.count + 1}
            if index + size < self.count:
                body['next'] = f'{self.base}/deezer/{request.match_info["kind"]}/42/tracks?limit={size}&index={index + size}'
            return web.json_response(body)

        # Apple Music ---------------------------------------------------
        @r.get('/apple/us/browse')
        async def am_page(request):
            return web.Response(text='<script type="module" src="/assets/index~abc123.js"></script>',
                                content_type='text/html')

        @r.get('/apple/assets/index~abc123.js')
        async def am_script(request):
            stale = _jwt({'iss': 'Other', 'exp': time.time() + 86400})
            return web.Response(text=f'a="{stale}";b="{self.apple_token}";')

        def am_auth(request):
            return (request.headers.get('Authorization') == f'Bearer {self.apple_token}'
                    and request.headers.get('Origin') == apple.SITE)

        @r.get('/amp/{sf}/playlists/{id}')
        async def am_playlist(request):
            if not am_auth(request):
                return web.Response(status=401)
            if request.match_info['id'] != 'pl.u-abc':
                return web.json_response({'errors': [{'status': '404', 'code': '40400'}]}, status=404)
            return web.json_response({'data': [{'attributes': {'name': 'Apple Mix', 'curatorName': 'someone'}}]})

        @r.get('/amp/{sf}/playlists/{id}/tracks')
        async def am_tracks(request):
            if not am_auth(request):
                return web.Response(status=401)
            offset, size = int(request.query['offset']), min(int(request.query['limit']), 300)
            self.calls.append(('apple', offset))
            items = [{'type': 'songs', 'attributes': {'name': s['title'], 'artistName': s['artist'],
                                                      'albumName': s['album'], 'durationInMillis': s['seconds'] * 1000,
                                                      'isrc': s['isrc']},
                      'relationships': {'artists': {'data': [{'attributes': {'name': s['artist']}}]}}}
                     for s in self.songs()[offset:offset + size]]
            body = {'data': items}
            if offset + size < self.count:
                body['next'] = f'/v1/catalog/us/playlists/pl.u-abc/tracks?offset={offset + size}'
            return web.json_response(body)

        @r.get('/amp/{sf}/songs/{id}')
        async def am_song(request):
            if not am_auth(request):
                return web.Response(status=401)
            return web.json_response({'data': [{'attributes': {
                'name': 'Choosin\' Texas', 'artistName': 'Ella Langley', 'albumName': 'Single', 'durationInMillis': 232000},
                'relationships': {'artists': {'data': [{'attributes': {'name': 'Ella Langley'}}]}}}]})

        # Tidal ---------------------------------------------------------
        @r.post('/tidal-auth')
        async def td_auth(request):
            form = await request.post()
            if form.get('client_id') != self.tidal_client or form.get('grant_type') != 'client_credentials':
                return web.json_response({'error': 'invalid_client'}, status=401)
            return web.json_response({'access_token': 'tidal-token', 'expires_in': 14400})

        @r.get('/tidal-embed/tracks/570268437')
        async def td_embed(request):
            return web.Response(text='<script src="/embed-resources/js/app-1.js"></script>', content_type='text/html')

        @r.get('/tidal-embed/embed-resources/js/app-1.js')
        async def td_app(request):
            return web.Response(text='import{a}from"../chunk-AB12.js";')

        @r.get('/tidal-embed/embed-resources/chunk-AB12.js')
        async def td_chunk(request):
            return web.Response(text='x={clientId:"NEWCLIENT",clientSecret:"newsecret",name:"TIDAL_Embed_Player"}')

        def td_track(s, i):
            return {'title': s['title'], 'version': 'Remix' if i == 1 else None, 'artists': [{'name': s['artist']}],
                    'album': {'title': s['album']}, 'duration': s['seconds'], 'isrc': s['isrc']}

        @r.get('/tidal/playlists/{id}')
        async def td_playlist(request):
            assert request.headers['Authorization'] == 'Bearer tidal-token'
            if request.match_info['id'] != '9412ba5b-4db0-4824-9f35-4f5f930f2738':
                return web.json_response({'status': 404, 'subStatus': 2001, 'userMessage': 'Playlist not found'},
                                         status=404)
            return web.json_response({'title': 'Tidal Mix', 'creator': {'id': 0}, 'type': 'EDITORIAL',
                                      'numberOfTracks': self.count})

        @r.get('/tidal/playlists/{id}/items')
        async def td_items(request):
            offset, size = int(request.query['offset']), int(request.query['limit'])
            assert size <= 100 and request.query['countryCode'] == 'US'
            self.calls.append(('tidal', offset))
            items = [{'type': 'track', 'item': td_track(s, offset + i)}
                     for i, s in enumerate(self.songs()[offset:offset + size])]
            return web.json_response({'items': items, 'totalNumberOfItems': self.count})

        @r.get('/tidal/tracks/{id}')
        async def td_song(request):
            return web.json_response(td_track(song(1), 1))

        # Qobuz ---------------------------------------------------------
        @r.get('/qobuz/playlist/get')
        async def qb_playlist(request):
            if request.query.get('app_id') != self.qobuz_app:
                return web.json_response({'status': 'error', 'code': 400,
                                          'message': 'Invalid or missing app_id parameter'}, status=400)
            pid = request.query['playlist_id']
            if pid == '40000000':
                return web.json_response({'status': 'error', 'code': 401, 'message':
                                          'Current authenticated user is not authorized to perform this request'},
                                         status=401)
            if pid != '64451968':
                return web.json_response({'status': 'error', 'code': 404,
                                          'message': 'No result matching given argument'}, status=404)
            offset, size = int(request.query['offset']), min(int(request.query['limit']), 100)
            self.calls.append(('qobuz', offset))
            items = [{'title': s['title'], 'performer': {'name': s['artist']},
                      'performers': f'{s["artist"]}, MainArtist - Someone Else, Composer',
                      'album': {'title': s['album']}, 'duration': s['seconds'], 'isrc': s['isrc']}
                     for s in self.songs()[offset:offset + size]]
            return web.json_response({'name': 'Qobuz Mix', 'owner': {'name': 'Rough Trade'},
                                      'tracks': {'items': items, 'total': self.count}})

        @r.get('/qobuz-open/')
        async def qb_open(request):
            return web.Response(text='<script src="/resources/2.2.3/js/main.js"></script>', content_type='text/html')

        @r.get('/qobuz-open/resources/2.2.3/js/main.js')
        async def qb_main(request):
            return web.Response(text='qobuzapi={app_id:"111"};qobuzapi={app_id:"999"}')

        # Pandora -------------------------------------------------------
        def pd_check(request):
            cookie = request.headers.get('Cookie', '')
            assert cookie == f'csrftoken={request.headers["X-CsrfToken"]}'

        @r.post('/pandora/v1/auth/anonymousLogin')
        async def pd_login(request):
            pd_check(request)
            return web.json_response({'authToken': 'pandora-token'})

        @r.post('/pandora/v7/playlists/getTracks')
        async def pd_tracks(request):
            pd_check(request)
            if request.headers.get('X-AuthToken') != 'pandora-token':
                return web.json_response({'errorType': 'INVALID_REQUEST'}, status=400)
            req = (await request.json())['request']
            assert 'bypassPrivacyRules' not in req
            if req['pandoraId'] != 'PL:1:2':
                return web.json_response({'errorType': 'PLAYLIST_NOT_FOUND', 'errorCode': 99001})
            if req['offset'] and req['playlistVersion'] != 7:
                return web.json_response({'errorType': 'PLAYLIST_VERSION_MISMATCH'}, status=400)
            self.calls.append(('pandora', req['offset']))
            ids = [f'TR:{i}' for i in range(req['offset'], min(req['offset'] + req['limit'], self.count))]
            # Details for only some of the songs: the rest must be looked up
            notes = {i: pd_song(int(i[3:])) for i in ids[:40]}
            notes['LI:2'] = {'type': 'LI', 'displayname': 'Paul'}
            return web.json_response({'name': 'Pandora Mix', 'totalTracks': self.count, 'version': 7,
                                      'tracks': [{'trackPandoraId': i} for i in ids], 'annotations': notes})

        def pd_song(i):
            s = song(i)
            return {'type': 'TR', 'name': s['title'], 'artistName': s['artist'], 'albumName': s['album'],
                    'duration': s['seconds'], 'isrc': s['isrc']}

        @r.post('/pandora/v4/catalog/annotateObjects')
        async def pd_annotate(request):
            pd_check(request)
            ids = (await request.json())['pandoraIds']
            assert len(ids) <= 100
            return web.json_response({i: pd_song(int(i[3:])) for i in ids if i.startswith('TR:')})

        # Beatport ------------------------------------------------------
        @r.post('/beatport-token/')
        async def bp_token(request):
            form = await request.post()
            assert form['client_id'] == beatport.CLIENT[0]
            return web.json_response({'access_token': 'bp-token', 'expires_in': 600})

        @r.get('/beatport/charts/{id}/')
        async def bp_chart(request):
            if request.match_info['id'] != '908164':
                return web.json_response({'detail': 'No Chart matches the given query.'}, status=404)
            return web.json_response({'name': 'Best New', 'person': {'owner_name': 'Beatport'}, 'track_count': self.count})

        @r.get('/beatport/charts/{id}/tracks/')
        async def bp_tracks(request):
            assert request.headers['Authorization'] == 'Bearer bp-token'
            page, size = int(request.query['page']), int(request.query['per_page'])
            start = (page - 1) * size
            self.calls.append(('beatport', start))
            items = [{'name': s['title'], 'mix_name': 'Original Mix', 'artists': [{'name': s['artist']}],
                      'release': {'name': s['album']}, 'length_ms': s['seconds'] * 1000, 'isrc': s['isrc']}
                     for s in self.songs()[start:start + size]]
            nxt = f'{self.base}/beatport/charts/908164/tracks/?page={page + 1}&per_page={size}' \
                if start + size < self.count else None
            return web.json_response({'results': items, 'next': nxt, 'count': self.count})

        # Amazon Music ---------------------------------------------------
        @r.post('/amz/{domain}/config.json')
        async def az_config(request):
            return web.json_response({'deviceId': '13500000000000000', 'sessionId': '140-1', 'version': '1.0',
                                      'csrf': {'token': 't', 'ts': '1', 'rnd': '2'},
                                      'siteRegion': 'EU' if request.match_info['domain'].endswith('.de') else 'NA'})

        def az_page(name, owner, count, items):
            table = {'interface': 'Web.TemplatesInterface.v1_0.Touch.WidgetsInterface.DescriptiveTableWidgetElement',
                     'items': items, 'onEndOfWidget': []}
            return {'methods': [{'template': {
                'interface': 'Web.TemplatesInterface.v1_0.Touch.DetailTemplateInterface.DetailTemplate',
                'headerText': {'text': name}, 'headerPrimaryText': owner, 'headerTertiaryText': f'{count} SONGS • 1 HR',
                'widgets': [table]}}]}

        @r.post('/mesk/{region}/{endpoint}')
        async def az_mesk(request):
            assert 'gzip' in request.headers.get('Accept-Encoding', '')
            body = json.loads(await request.text())    # sent as text/plain, as the web player does
            headers = json.loads(body['headers'])
            assert headers['x-amzn-device-id'] == '13500000000000000'
            endpoint, xid = request.match_info['endpoint'], body['id']
            self.calls.append(('amazon', request.match_info['region'], endpoint, headers['x-amzn-music-domain']))
            if endpoint == 'showCatalogPlaylist' and xid == 'B01LS62KK2':
                items = [{'primaryText': s['title'] + (' [Explicit]' if i == 2 else ''), 'secondaryText1': s['artist'],
                          'secondaryText2': s['album'], 'secondaryText3': f'0{s["seconds"] // 60}:{s["seconds"] % 60:02}',
                          'primaryLink': {'deeplink': f'/albums/B0000000{i:02}?trackAsin=B0000001{i:02}'}}
                         for i, s in enumerate(self.songs())]
                return web.json_response(az_page('Classic Rock', 'Curated by Amazon Music', self.count, items))
            if endpoint == 'showCatalogAlbum' and xid == 'B07VX8D1QS':
                items = [{'primaryText': 'Come Together', 'secondaryText2': '', 'secondaryText3': '04:20',
                          'primaryLink': {'deeplink': '/albums/B07VX8D1QS?trackAsin=B07VX9QYF6'}},
                         {'primaryText': 'Something', 'secondaryText2': 'Guest', 'secondaryText3': '03:03',
                          'primaryLink': {'deeplink': '/albums/B07VX8D1QS?trackAsin=B07VX9QYF7'}}]
                return web.json_response(az_page('Abbey Road', 'The Beatles', 2, items))
            return web.json_response({'methods': [
                {'template': {'interface': 'Web.TemplatesInterface.v1_0.Touch.GalleryTemplateInterface.GalleryTemplate'}},
                {'interface': 'TemplateListInterface.v1_0.ShowNotificationMethod', 'notification': {
                    'interface': 'RibbonNotificationMessageElement',
                    'text': 'We\'re sorry, but this playlist is no longer available.'}}]})

        # A share link that redirects, and one that answers with a page that sends the browser on
        @r.get('/s/redirect')
        async def short_redirect(request):
            raise web.HTTPFound('/landing?to=https://www.deezer.com/en/track/5')

        @r.get('/landing')
        async def landing(request):
            return web.Response(text='welcome')

        @r.get('/s/page')
        async def short_page(request):
            return web.Response(text='<script>window.location="https:\\/\\/www.deezer.com\\/playlist\\/42"</script>',
                                content_type='text/html')

        app = web.Application()
        app.add_routes(r)
        return app


@pytest_asyncio.fixture
async def services(monkeypatch):
    f = FakeServices()
    server = TestServer(f.app())
    await server.start_server()
    base = f.base = str(server.make_url('')).rstrip('/')
    monkeypatch.setattr(deezer, 'API', base + '/deezer')
    monkeypatch.setattr(apple, 'SITE', base + '/apple')
    monkeypatch.setattr(apple, 'API', base + '/amp/{sf}/')
    monkeypatch.setattr(tidal, 'API', base + '/tidal/')
    monkeypatch.setattr(tidal, 'AUTH', base + '/tidal-auth')
    monkeypatch.setattr(tidal, 'EMBED', base + '/tidal-embed')
    monkeypatch.setattr(tidal, 'EMBED_PAGE', base + '/tidal-embed/tracks/570268437')
    monkeypatch.setattr(qobuz, 'API', base + '/qobuz/')
    monkeypatch.setattr(qobuz, 'OPEN', base + '/qobuz-open')
    monkeypatch.setattr(pandora, 'API', base + '/pandora/')
    monkeypatch.setattr(beatport, 'API', base + '/beatport/')
    monkeypatch.setattr(beatport, 'TOKEN_URL', base + '/beatport-token/')
    monkeypatch.setattr(amazon, 'SITE', base + '/amz/{domain}')
    monkeypatch.setattr(amazon, 'MESK', base + '/mesk/{region}/')
    monkeypatch.setattr(links, 'SHORT', links.re.compile(r'http://127\.0\.0\.1:\d+/s/\w+'))
    yield f
    await server.close()


# Recognising links ------------------------------------------------------

LINKS = {
    # Apple Music
    'https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb':
        ('apple', 'playlist', 'us/pl.f4d106fed2bd41149aaacabb233eb5eb'),
    'https://music.apple.com/gb/playlist/pl.u-PJD9BskgpYKR': ('apple', 'playlist', 'gb/pl.u-PJD9BskgpYKR'),
    'https://music.apple.com/us/album/discovery/697194953': ('apple', 'album', 'us/697194953'),
    'https://music.apple.com/us/album/choosin-texas/1844932149?i=1844932150': ('apple', 'song', 'us/1844932150'),
    'https://music.apple.com/de/song/choosin-texas/1844932150': ('apple', 'song', 'de/1844932150'),
    'https://itunes.apple.com/us/album/id1844932149': ('apple', 'album', 'us/1844932149'),
    'https://music.apple.com/album/1844932149': ('apple', 'album', 'us/1844932149'),
    # Deezer
    'https://www.deezer.com/us/playlist/1313621735?utm_source=x': ('deezer', 'playlist', '1313621735'),
    'https://www.deezer.com/album/302127': ('deezer', 'album', '302127'),
    'https://deezer.com/pt-br/track/3135556': ('deezer', 'song', '3135556'),
    # Tidal
    'https://tidal.com/browse/playlist/9412ba5b-4db0-4824-9f35-4f5f930f2738':
        ('tidal', 'playlist', '9412ba5b-4db0-4824-9f35-4f5f930f2738'),
    'https://listen.tidal.com/album/2854643': ('tidal', 'album', '2854643'),
    'https://tidal.com/track/570268437/u': ('tidal', 'song', '570268437'),
    'https://tidal.com/browse/album/2854643/track/570268437': ('tidal', 'song', '570268437'),
    # Qobuz
    'https://open.qobuz.com/playlist/64451968': ('qobuz', 'playlist', '64451968'),
    'https://www.qobuz.com/us-en/playlists/counter-culture/64451968': ('qobuz', 'playlist', '64451968'),
    'https://play.qobuz.com/album/w8gvelqm43sqy': ('qobuz', 'album', 'w8gvelqm43sqy'),
    'https://www.qobuz.com/us-en/album/peeling-an-orange/w8gvelqm43sqy': ('qobuz', 'album', 'w8gvelqm43sqy'),
    'https://open.qobuz.com/track/460530495': ('qobuz', 'song', '460530495'),
    # Pandora
    'https://www.pandora.com/playlist/PL:73406696952697834:1133813906':
        ('pandora', 'playlist', 'PL:73406696952697834:1133813906'),
    'https://www.pandora.com/artist/alicia-keys/as-i-am-expanded-edition/ALlfwXKdgq7JvPg':
        ('pandora', 'album', 'ALlfwXKdgq7JvPg'),
    'https://www.pandora.com/artist/alicia-keys/as-i-am/like-youll-never-see-me-again/TRrrqx5vtdPqc7X':
        ('pandora', 'song', 'TRrrqx5vtdPqc7X'),
    # Amazon Music
    'https://music.amazon.com/playlists/B01LS62KK2?ref=dm_sh_abc': ('amazon', 'playlist', 'music.amazon.com/B01LS62KK2'),
    'https://music.amazon.com/user-playlists/5e4e27ef693e4166ab927c348088377esune':
        ('amazon', 'playlist', 'music.amazon.com/user:5e4e27ef693e4166ab927c348088377esune'),
    'https://music.amazon.co.uk/albums/B07VX8D1QS': ('amazon', 'album', 'music.amazon.co.uk/B07VX8D1QS'),
    'https://music.amazon.com.au/albums/B07VX8D1QS?trackAsin=B07VX9QYF6&do=play':
        ('amazon', 'song', 'music.amazon.com.au/B07VX8D1QS?B07VX9QYF6'),
    'https://www.amazon.de/music/player/playlists/B00ZPISIW4': ('amazon', 'playlist', 'music.amazon.de/B00ZPISIW4'),
    # Beatport
    'https://www.beatport.com/chart/best-new-independent-artists-october-2026/908164': ('beatport', 'playlist', '908164'),
    'https://www.beatport.com/de/release/goa-psytrance-party/930093': ('beatport', 'album', '930093'),
    'https://www.beatport.com/track/you-cant-make-love/31262653': ('beatport', 'song', '31262653'),
    # Spotify and YouTube still work
    'https://open.spotify.com/album/2noRn2Aes5aoNVsU6iWThc?si=1': ('spotify', 'album', '2noRn2Aes5aoNVsU6iWThc'),
    'https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC': ('spotify', 'song', '4uLU6hMCjMI75M1A2tKUQC'),
    'https://music.youtube.com/watch?v=SMVqAJcX9I4': ('youtube', 'song', 'SMVqAJcX9I4'),
}


def test_links_are_recognised():
    for text, want in LINKS.items():
        link = links.find(f'listen to this: {text} !')
        assert link and (link.service, link.kind, link.id) == want, text


def test_links_that_cant_be_read_say_why():
    for text, words in (('https://www.pandora.com/station/play/123', 'station'),
                        ('https://www.beatport.com/playlists/share/11707075', 'Beatport playlists'),
                        ('https://music.amazon.com/tracks/B0D74KX9RR', 'Share button'),
                        ('https://music.apple.com/us/artist/drake/271256', 'not artists'),
                        ('https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi', 'not artists'),
                        ('https://example.com/playlist/123', 'Paste a link from'),
                        ('daft punk', 'Paste a link from')):
        with pytest.raises(UserError) as e:
            links.check(text)
        assert words in str(e.value), text
    assert links.check('https://link.deezer.com/s/31yJ4dRdbZVL1fK6WjdBz') is None   # to be followed


# Reading them -----------------------------------------------------------

async def read(text):
    return await links.Readers().read(links.find(text))


def assert_whole(found, name, owner, count=250):
    assert (found.name, found.owner, len(found.rows)) == (name, owner, count)
    assert [r['Track Name'] for r in found.rows[:3]][0] == 'Song 0'
    tracks, skipped = playlist.parse(found.csv().encode(), limit=10_000)
    assert (len(tracks), skipped) == (count, 0)
    # Artists are joined with ";" so a band with a comma in its name stays whole
    assert (tracks[0].artist, tracks[0].album, tracks[0].duration) == ('Earth, Wind & Fire', 'Album 0', 200)


async def test_deezer_playlist_is_paged_and_counts_missing_songs(services):
    found = await read('https://www.deezer.com/playlist/42')
    assert_whole(found, 'Deezer Mix', 'dz')
    assert found.unavailable == 1
    assert len([c for s, c in services.calls if s == 'deezer']) == 1   # 250 fit in one page of 400

    services.count = 900
    services.calls.clear()
    found = await read('https://www.deezer.com/playlist/42')
    assert len(found.rows) == 900 and [c for s, c in services.calls] == [0, 400, 800]

    with pytest.raises(UserError) as e:
        await read('https://www.deezer.com/playlist/1')
    assert e.value.status == 404 and 'private' in str(e.value)
    s = await read('https://www.deezer.com/track/5')
    assert (s.title, s.artist, s.album, s.duration) == ('Digital Love', 'Daft Punk', 'Discovery', 301)


async def test_apple_music_token_comes_from_the_web_player_and_pages_past_300(services):
    services.count = 650
    reader = links.Readers()
    found = await reader.read(links.find('https://music.apple.com/us/playlist/mix/pl.u-abc'))
    assert_whole(found, 'Apple Mix', 'someone', 650)
    assert [c for s, c in services.calls] == [0, 300, 600]

    # Apple moves on to a new token: the old one gets 401, and the new one is read from the script
    services.apple_token = _jwt({'iss': 'AMPWebPlay', 'exp': time.time() + 86400 * 70})
    s = await reader.read(links.find('https://music.apple.com/us/album/x/1844932149?i=1844932150'))
    assert (s.title, s.artist) == ('Choosin\' Texas', 'Ella Langley')

    with pytest.raises(UserError) as e:
        await reader.read(links.find('https://music.apple.com/us/playlist/x/pl.nothere'))
    assert e.value.status == 404


async def test_tidal_gets_a_token_and_reads_a_new_client_from_the_embed_player(services):
    found = await read('https://tidal.com/browse/playlist/9412ba5b-4db0-4824-9f35-4f5f930f2738')
    assert_whole(found, 'Tidal Mix', 'TIDAL')
    assert [c for s, c in services.calls] == [0, 100, 200]
    assert found.rows[1]['Track Name'] == 'Song 1 (Remix)'

    services.tidal_client = 'NEWCLIENT'   # Tidal turned the old client down
    reader = links.Readers()
    s = await reader.read(links.find('https://tidal.com/track/1'))
    assert (s.title, s.artist) == ('Song 1 (Remix)', 'Artist 1')
    assert reader.readers['tidal'].client == ('NEWCLIENT', 'newsecret')

    with pytest.raises(UserError) as e:
        await read('https://tidal.com/browse/playlist/00000000-0000-0000-0000-000000000000')
    assert e.value.status == 404


async def test_qobuz_pages_and_finds_a_new_app_id(services):
    services.qobuz_app = '999'      # Qobuz turned the old app id down; open.qobuz.com has the new one
    found = await read('https://open.qobuz.com/playlist/64451968')
    assert_whole(found, 'Qobuz Mix', 'Rough Trade')
    assert [c for s, c in services.calls] == [0, 100, 200]
    for pid in ('40000000', '1'):    # private, missing
        with pytest.raises(UserError) as e:
            await read(f'https://open.qobuz.com/playlist/{pid}')
        assert e.value.status == 404


async def test_pandora_pages_by_version_and_looks_up_missing_details(services):
    found = await read('https://www.pandora.com/playlist/PL:1:2')
    assert_whole(found, 'Pandora Mix', 'Paul')
    assert [c for s, c in services.calls] == [0, 100, 200]
    with pytest.raises(UserError) as e:
        await read('https://www.pandora.com/playlist/PL:3:4')
    assert e.value.status == 404


async def test_beatport_chart_follows_next_links(services):
    found = await read('https://www.beatport.com/chart/best-new/908164')
    assert (found.name, found.owner, len(found.rows)) == ('Best New', 'Beatport', 250)
    assert found.rows[0]['Track Name'] == 'Song 0 (Original Mix)'
    assert [c for s, c in services.calls] == [0, 100, 200]
    with pytest.raises(UserError) as e:
        await read('https://www.beatport.com/chart/x/1')
    assert e.value.status == 404


async def test_amazon_music_reads_a_whole_playlist_in_one_answer(services):
    found = await read('https://music.amazon.com/playlists/B01LS62KK2')
    assert_whole(found, 'Classic Rock', 'Amazon Music')
    assert found.rows[2]['Track Name'] == 'Song 2'    # "[Explicit]" is left off
    assert services.calls == [('amazon', 'na', 'showCatalogPlaylist', 'music.amazon.com')]

    s = await read('https://www.amazon.de/music/player/albums/B07VX8D1QS?trackAsin=B07VX9QYF7')
    assert (s.title, s.artist, s.album, s.duration) == ('Something', 'Guest', 'Abbey Road', 183)
    assert services.calls[-1] == ('amazon', 'eu', 'showCatalogAlbum', 'music.amazon.de')
    found = await read('https://music.amazon.com/albums/B07VX8D1QS')
    assert [(r['Track Name'], r['Artist Name(s)']) for r in found.rows] == [('Come Together', 'The Beatles'),
                                                                            ('Something', 'Guest')]
    for link in ('https://music.amazon.com/playlists/B01M2AC326', 'https://music.amazon.com/albums/B07VX8D1QS?trackAsin=B000000000'):
        with pytest.raises(UserError) as e:
            await read(link)
        assert e.value.status == 404


async def test_share_links_are_followed(services):
    link = await links.resolve(f'{services.base}/s/redirect')
    assert (link.service, link.kind, link.id) == ('deezer', 'song', '5')
    link = await links.resolve(f'look: {services.base}/s/page')
    assert (link.service, link.kind, link.id) == ('deezer', 'playlist', '42')


# The page's endpoint --------------------------------------------------------

async def test_link_endpoint_gives_a_csv_or_a_search(site, services):
    services.count = 3
    res = await site.post('/api/link', json={'url': 'https://www.deezer.com/en/playlist/42'})
    assert res.status == 200, await res.text()
    body = await res.json()
    assert (body['kind'], body['source'], body['sourceName'], body['name'], body['total'], body['unavailable'],
            body['filename']) == ('playlist', 'deezer', 'Deezer', 'Deezer Mix', 3, 1, 'Deezer Mix.csv')
    job = await scan(site, body['csv'], 'flac')
    assert job['total'] == 3

    res = await site.post('/api/link', json={'url': f'{services.base}/s/redirect'})
    body = await res.json()
    assert (body['kind'], body['query'], body['video']) == ('song', 'Daft Punk Digital Love', None)

    res = await site.post('/api/link', json={'url': 'https://www.pandora.com/station/play/1'})
    assert res.status == 400 and 'station' in (await res.json())['error']


def test_original_mix_is_left_out_of_the_search():
    # Beatport calls the usual version of a track its "Original Mix"; files seldom say so
    assert playlist.Track('You Cant Make Love (Original Mix)', 'The Unkool Hillbillies').query == \
        'The Unkool Hillbillies You Cant Make Love'
    assert playlist.clean_title('Strobe - Original Mix') == 'Strobe'
    # Another mix is another version, and stays
    assert playlist.clean_title('Murderer (Extended Mix)') == 'Murderer (Extended Mix)'
    assert playlist.clean_title('Original Mixtape') == 'Original Mixtape'
