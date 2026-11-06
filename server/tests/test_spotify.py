"""Reading a Spotify playlist link, against a fake Spotify."""

import json

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestServer

from songdl import playlist, spotify
from songdl.errors import UserError

from tests.test_site import scan

GOOD = 'a' * 64
OLD = 'b' * 64
PID = '37i9dQZF1DWXRqgorJj26U'
AID = '2noRn2Aes5aoNVsU6iWThc'
TID = '4uLU6hMCjMI75M1A2tKUQC'


class FakeSpotify:
    def __init__(self, count=250):
        self.count = count
        self.hash = GOOD          # the one hash the query endpoint accepts
        self.short_page = None    # an offset to answer with too few items
        self.queries = []
        self.url = None

    def item(self, i):
        artists = [{'profile': {'name': 'Earth, Wind & Fire'}}, {'profile': {'name': 'Guest'}}] if i == 0 else \
            [{'profile': {'name': f'Artist {i}'}}]
        return {'addedAt': {'isoString': '2026-01-01T00:00:00Z'}, 'itemV2': {
            '__typename': 'TrackResponseWrapper',
            'data': {'__typename': 'Track', 'uri': f'spotify:track:{i}', 'name': f'Song {i}',
                     'artists': {'items': artists}, 'albumOfTrack': {'name': f'Album {i}'},
                     'trackDuration': {'totalMilliseconds': 200_000 + i}}}}

    def app(self):
        async def embed(request):
            if request.match_info['kind'] == 'track' and request.match_info['id'] != TID:
                data = {'props': {'pageProps': {'status': 404, 'title': 'Page not found'}}}
                return web.Response(text=f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>',
                                    content_type='text/html')
            data = {'props': {'pageProps': {'state': {'settings': {'session': {
                'accessToken': 'tok', 'accessTokenExpirationTimestampMs': 9_999_999_999_000}},
                'data': {'entity': {'type': 'track', 'name': 'Never Gonna Give You Up', 'duration': 213573,
                                    'artists': [{'name': 'Rick Astley'}]}}}}}}
            return web.Response(text=f'<html><script id="__NEXT_DATA__" type="application/json">'
                                     f'{json.dumps(data)}</script></html>', content_type='text/html')

        async def query(request):
            assert request.headers['Authorization'] == 'Bearer tok'
            sha = json.loads(request.query['extensions'])['persistedQuery']['sha256Hash']
            if sha != self.hash:
                return web.json_response({'errors': [{'message': 'PersistedQueryNotFound'}]})
            v = json.loads(request.query['variables'])
            self.queries.append(v['offset'])
            if request.query['operationName'] == 'getAlbum':
                if not v['uri'].endswith(AID):
                    return web.json_response({'data': {'albumUnion': {'__typename': 'NotFound'}}})
                end = min(v['offset'] + v['limit'], self.count)
                items = [{'track': self.item(i)['itemV2']['data']} for i in range(v['offset'], end)]
                return web.json_response({'data': {'albumUnion': {
                    '__typename': 'Album', 'name': 'Big Album', 'artists': {'items': [{'profile': {'name': 'Band'}}]},
                    'tracksV2': {'items': items, 'totalCount': self.count}}}})
            if not v['uri'].endswith(PID):
                return web.json_response({'data': {'playlistV2': {'__typename': 'NotFound', 'message': 'nope'}}})
            end = min(v['offset'] + v['limit'], self.count)
            if v['offset'] == self.short_page:
                end -= 1
            items = [self.item(i) for i in range(v['offset'], end)]
            return web.json_response({'data': {'playlistV2': {
                '__typename': 'Playlist', 'name': 'Road Trip / 2026', 'ownerV2': {'data': {'name': 'someone'}},
                'content': {'items': items, 'totalCount': self.count}}}})

        async def player(request):
            return web.Response(text=f'<script src="https://open.spotifycdn.com/cdn/build/web-player/web-player.abc123.js">',
                                content_type='text/html')

        async def script(request):
            return web.Response(text=f'x=new aZ.l("fetchPlaylist","query","{GOOD}",null)')

        app = web.Application()
        app.router.add_get('/embed/{kind}/{id}', embed)
        app.router.add_get('/query', query)
        app.router.add_get('/', player)
        app.router.add_get('/script.js', script)
        return app


@pytest_asyncio.fixture
async def fake_spotify(monkeypatch):
    f = FakeSpotify()
    server = TestServer(f.app())
    await server.start_server()
    base = str(server.make_url('')).rstrip('/')
    monkeypatch.setattr(spotify, 'EMBED_URL', base + '/embed/{kind}/{id}')
    monkeypatch.setattr(spotify, 'QUERY_URL', base + '/query')
    monkeypatch.setattr(spotify, 'PLAYER_URL', base + '/')
    monkeypatch.setattr(spotify, 'HASHES', {'fetchPlaylist': [OLD, GOOD], 'getAlbum': [GOOD]})
    # The web player's script lives on another host; send that to the fake too
    real_get = spotify.aiohttp.ClientSession.get

    def get(self, url, *a, **kw):
        if str(url).startswith('https://open.spotifycdn.com/'):
            url = base + '/script.js'
        return real_get(self, url, *a, **kw)

    monkeypatch.setattr(spotify.aiohttp.ClientSession, 'get', get)
    yield f
    await server.close()


def test_playlist_links_are_recognised():
    for link in (f'https://open.spotify.com/playlist/{PID}?si=1a2b3c',
                 f'open.spotify.com/intl-de/playlist/{PID}',
                 f'https://open.spotify.com/embed/playlist/{PID}',
                 f'https://open.spotify.com/user/someone/playlist/{PID}',
                 f'spotify:playlist:{PID}',
                 f'Check this out: https://open.spotify.com/playlist/{PID} !'):
        assert spotify.playlist_id(link) == PID, link
    for link in (f'https://open.spotify.com/track/{PID}', 'https://open.spotify.com/playlist/short',
                 f'https://example.com/playlist/{PID}', 'daft punk digital love'):
        assert spotify.playlist_id(link) is None, link


async def test_the_whole_playlist_is_read_past_100_songs(fake_spotify):
    found = await spotify.Spotify().playlist(PID)
    assert (found.name, found.owner, len(found.rows)) == ('Road Trip / 2026', 'someone', 250)
    assert sorted(fake_spotify.queries) == [0, 100, 200]
    assert [r['Track Name'] for r in found.rows] == [f'Song {i}' for i in range(250)]

    tracks, skipped = playlist.parse(found.csv().encode())
    assert (len(tracks), skipped) == (250, 0)
    # Artists are joined with ";" so a band with a comma in its name stays whole
    assert (tracks[0].artist, tracks[0].album, tracks[0].duration) == ('Earth, Wind & Fire', 'Album 0', 200)


async def test_a_retired_query_hash_is_replaced_from_the_web_player(fake_spotify, monkeypatch):
    monkeypatch.setattr(spotify, 'HASHES', {'fetchPlaylist': [OLD], 'getAlbum': [OLD]})
    reader = spotify.Spotify()
    assert len((await reader.playlist(PID)).rows) == 250
    assert reader.hashes['fetchPlaylist'][0] == GOOD


async def test_a_whole_album_is_read_50_songs_at_a_time(fake_spotify):
    fake_spotify.count = 120
    found = await spotify.Spotify().album(AID)
    assert (found.name, found.owner, len(found.rows)) == ('Big Album', 'Band', 120)
    assert sorted(fake_spotify.queries) == [0, 50, 100]
    assert {r['Album Name'] for r in found.rows} == {'Big Album'}
    with pytest.raises(UserError) as e:
        await spotify.Spotify().album('0' * 22)
    assert e.value.status == 404


async def test_a_song_link_is_read_from_its_embed_page(fake_spotify):
    found = await spotify.Spotify().song(TID)
    assert (found.title, found.artist, found.duration) == ('Never Gonna Give You Up', 'Rick Astley', 214)
    with pytest.raises(UserError) as e:
        await spotify.Spotify().song('0' * 22)
    assert e.value.status == 404


async def test_a_short_answer_is_an_error_not_a_short_csv(fake_spotify):
    fake_spotify.short_page = 100
    with pytest.raises(UserError) as e:
        await spotify.Spotify().playlist(PID)
    assert e.value.status == 502


async def test_a_missing_playlist_is_reported(fake_spotify):
    with pytest.raises(UserError) as e:
        await spotify.Spotify().playlist('0' * 22)
    assert e.value.status == 404 and 'private' in str(e.value)


async def test_playlist_link_endpoint_gives_a_csv_that_scans(site, fake_spotify):
    fake_spotify.count = 3
    res = await site.post('/api/link', json={'url': f'https://open.spotify.com/playlist/{PID}?si=x'})
    assert res.status == 200, await res.text()
    body = await res.json()
    assert (body['name'], body['total'], body['filename'], body['maxScan']) == \
        ('Road Trip / 2026', 3, 'Road Trip _ 2026.csv', playlist.MAX_TRACKS)
    job = await scan(site, body['csv'], 'flac')
    assert job['total'] == 3

    res = await site.post('/api/link', json={'url': 'https://open.spotify.com/artist/4tZwfgrHOc3mvqYlEYSvVi'})
    assert res.status == 400
