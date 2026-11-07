"""Sync playlist: a playlist added from a link gets the songs added to it there since, and
never a song twice."""

import asyncio
import json

import pytest
import pytest_asyncio

from songdl import links, spotify
from songdl.playlist import Track
from songdl.sync import _same_song, song_key

from tests.conftest import start_site
from tests.test_collection import login, make_wav
from tests.test_imports import import_view
from tests.test_site import queue_view, wait_for

LINK = 'https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc'


class Remote:
    """The playlist on Spotify, as the test changes it."""

    def __init__(self, songs):
        self.songs = list(songs)   # (artist, title)
        self.reads = 0
        self.delay = 0

    async def read(self, readers, link):
        assert (link.service, link.kind, link.id) == ('spotify', 'playlist', '37i9dQZF1DXcBWIGoYBM5M')
        self.reads += 1
        await asyncio.sleep(self.delay)
        rows = [{'Track URI': f'spotify:track:{i}', 'Track Name': title, 'Artist Name(s)': artist,
                 'Album Name': '', 'Duration (ms)': '', 'Added At': ''} for i, (artist, title) in enumerate(self.songs)]
        return spotify.Playlist('37i9dQZF1DXcBWIGoYBM5M', 'Road Trip', 'me', rows)

    def csv(self):
        return 'title,artist\n' + ''.join(f'{t},{a}\n' for a, t in self.songs)


@pytest.fixture
def remote(monkeypatch):
    r = Remote([('Daft Punk', 'Digital Love'), ('Nobody', 'No Such Song')])
    monkeypatch.setattr(links.Readers, 'read', lambda readers, link: r.read(readers, link))
    return r


@pytest_asyncio.fixture
async def music(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path / 'music')
    await login(client)
    yield client
    await client.close()


async def import_linked(client, remote, playlist='Road Trip', link=LINK, dj=False):
    res = await client.post('/api/imports', json={'csv': remote.csv(), 'name': 'Road Trip', 'formats': 'wav,flac',
                                                  'playlist': playlist, 'link': link, 'dj': dj})
    assert res.status == 200, await res.text()
    return (await res.json())['id']


async def library(client):
    return {p['name']: p for p in (await (await client.get('/api/library')).json())['playlists']}


async def sync(client, name='Road Trip', status=200):
    res = await client.post('/api/library/sync', json={'playlist': name})
    assert res.status == status, await res.text()
    return await res.json()


async def queued_titles(client):
    return sorted(i['title'] for i in (await queue_view(client))['items'])


async def test_sync_downloads_only_what_was_added_there(music, remote, tmp_path):
    iid = await import_linked(music, remote)
    await wait_for(lambda: import_view(music, iid), lambda v: v['state'] == 'done')
    await wait_for(lambda: queue_view(music), lambda q: all(i['status'] == 'done' for i in q['items']), timeout=30)
    assert (tmp_path / 'music' / 'Road Trip' / 'Daft Punk - Digital Love.wav').exists()

    link = (await library(music))['Road Trip']['link']
    assert link['source'] == 'spotify' and link['sourceName'] == 'Spotify' and link['url'] == LINK
    assert link['synced'] is None

    # Nothing changed there: nothing to do, and the song never found isn't searched again
    res = await sync(music)
    assert (res['added'], res['queued'], res['import']) == (0, 0, None)
    assert res['link']['synced'] and res['link']['added'] == 0

    # Two songs added there (one of them twice), and one taken off: the removal isn't counted
    remote.songs = [('Daft Punk', 'Digital Love'), ('Duck Sauce', 'Barbra Streisand'),
                    ('Daft Punk', 'One More Time'), ('Daft Punk', 'One More Time (feat. Nobody)')]
    res = await sync(music)
    assert (res['total'], res['added'], res['already'], res['queued']) == (4, 2, 0, 2)
    imp = res['import']
    assert imp['name'] == 'Road Trip (sync)' and imp['folder'] == 'Road Trip/' and imp['total'] == 2
    assert imp['formats'] == ['WAV', 'FLAC']
    await wait_for(lambda: import_view(music, imp['id']), lambda v: v['state'] == 'done')
    assert await queued_titles(music) == ['Barbra Streisand', 'Digital Love', 'One More Time']

    # Synced again straight away: those two are known now, even before they've downloaded
    res = await sync(music)
    assert (res['added'], res['queued']) == (0, 0)

    # A song deleted here isn't new there, so it doesn't come back
    await wait_for(lambda: queue_view(music), lambda q: all(i['status'] == 'done' for i in q['items']), timeout=30)
    res = await music.delete('/api/library/song', params={'playlist': 'Road Trip', 'path': 'Daft Punk - Digital Love.wav'})
    assert res.status == 200
    assert (await sync(music))['added'] == 0

    saved = json.loads((tmp_path / 'data' / 'playlist-links.json').read_text())
    assert 'Road Trip' in saved['playlists']


async def test_a_new_song_already_in_the_folder_is_counted_but_not_fetched(music, remote, tmp_path):
    await import_linked(music, remote)
    # Saved by hand, spelled a little differently from Spotify
    make_wav(tmp_path / 'music' / 'Road Trip' / 'Kavinsky - Nightcall.wav')
    remote.songs.append(('Kavinsky', 'Nightcall - Remastered 2010'))
    remote.songs.append(('Radiohead', 'Weird Fishes Arpeggi'))
    res = await sync(music)
    assert (res['added'], res['already'], res['queued']) == (2, 1, 1)
    await wait_for(lambda: import_view(music, res['import']['id']), lambda v: v['state'] == 'done')
    assert 'Nightcall' not in await queued_titles(music)
    assert 'Weird Fishes Arpeggi' in await queued_titles(music)


async def test_one_sync_at_a_time(music, remote, tmp_path):
    await import_linked(music, remote)
    (tmp_path / 'music' / 'Road Trip').mkdir(parents=True, exist_ok=True)
    remote.songs.append(('Kavinsky', 'Nightcall'))
    remote.delay = 0.5
    first = asyncio.ensure_future(sync(music))
    await asyncio.sleep(0.1)
    res = await sync(music, status=409)
    assert 'already' in res['error']
    assert (await first)['queued'] == 1
    assert (await sync(music))['added'] == 0


async def test_a_dj_playlist_syncs_into_its_dj_folder_as_dj_copies(music, remote, tmp_path):
    await import_linked(music, remote, dj=True)
    (tmp_path / 'music' / 'Road Trip (DJ)').mkdir(parents=True, exist_ok=True)
    assert (await library(music))['Road Trip (DJ)']['link']
    remote.songs.append(('Kavinsky', 'Nightcall'))
    imp = (await sync(music, 'Road Trip (DJ)'))['import']
    assert imp['dj'] and imp['folder'] == 'Road Trip (DJ)/'


async def test_only_a_playlist_from_a_link_can_sync(music, remote, tmp_path):
    make_wav(tmp_path / 'music' / 'Mixtape' / 'Daft Punk - Digital Love.wav')
    # A CSV import, no link
    res = await music.post('/api/imports', json={'csv': remote.csv(), 'name': 'Mixtape.csv', 'formats': 'wav',
                                                 'playlist': 'Mixtape'})
    assert res.status == 200
    assert (await library(music))['Mixtape']['link'] is None
    res = await sync(music, 'Mixtape', status=409)
    assert 'link' in res['error']
    await sync(music, 'Nowhere', status=404)
    assert remote.reads == 0


async def test_sync_needs_a_login(site):
    res = await site.post('/api/library/sync', json={'playlist': 'Road Trip'})
    assert res.status == 401


async def test_a_scanned_link_saved_to_the_server_can_sync(music, remote, tmp_path):
    res = await music.post('/api/scan?name=Road%20Trip.csv&formats=wav', data=remote.csv().encode())
    sid = (await res.json())['id']
    job = await wait_for(lambda: _scan(music, sid),
                         lambda j: j['state'] != 'running')
    ids = [r['id'] for t in job['tracks'] for r in t.get('results', [])]
    assert ids
    res = await music.post('/api/queue', json={'playlist': 'Road Trip', 'results': ids, 'link': LINK, 'scan': sid})
    assert res.status == 200
    (tmp_path / 'music' / 'Road Trip').mkdir(parents=True, exist_ok=True)
    assert (await library(music))['Road Trip']['link']['url'] == LINK
    # Everything the scan had is known, found or not
    assert (await sync(music))['added'] == 0
    remote.songs.append(('Kavinsky', 'Nightcall'))
    assert (await sync(music))['queued'] == 1


async def _scan(client, sid):
    return await (await client.get(f'/api/scan/{sid}')).json()


def test_one_song_however_it_is_spelled():
    assert song_key('Digital Love', 'Daft Punk') == song_key('Digital Love (feat. DJ Falcon)', 'daft punk')
    assert song_key('Nightcall - Remastered 2010', 'Kavinsky') == song_key('Nightcall', 'Kavinsky')
    assert song_key('Río', 'X') == song_key('Rio', 'x')
    assert song_key('Digital Love - Remix', 'Daft Punk') != song_key('Digital Love', 'Daft Punk')
    assert song_key('One More Time', 'Daft Punk') != song_key('One More Time', 'Someone Else')

    track = Track('Digital Love', 'Daft Punk')
    assert _same_song(track, {'title': 'Digital Love', 'artist': 'Daft Punk, DJ Falcon'})
    assert _same_song(track, {'title': 'Digital Love', 'artist': ''})
    assert not _same_song(track, {'title': 'Digital Love', 'artist': 'Someone Else'})
    assert not _same_song(track, {'title': 'Love', 'artist': 'Daft Punk'})
