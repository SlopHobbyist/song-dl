"""The Library page's API: playlists saved on the server, their songs, downloading and deleting them."""

import io
import wave
import zipfile

from tests.conftest import ADMIN, start_site

UUID = '0f8fad5b-d9cb-469f-a165-70867728950e'


def make_wav(path, seconds=2, rate=44100):   # over the 200 KB library.py takes in
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b'\0\0' * 2 * rate * seconds)


async def login(client):
    res = await client.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
    assert res.status == 200


async def test_playlists_are_the_server_folders_but_not_the_downloads(tmp_path, fake):
    # The downloads folder inside the server folder, as music/downloads on the real server
    client = await start_site(tmp_path, fake, server_dir=tmp_path)
    try:
        assert (await client.get('/api/library')).status == 401
        make_wav(tmp_path / 'Road Trip' / 'Daft Punk - Digital Love.wav')
        make_wav(tmp_path / 'Road Trip' / 'Justice - Genesis.wav')
        (tmp_path / 'Road Trip' / 'cover.jpg').write_bytes(b'x')
        make_wav(tmp_path / 'Empty (DJ)' / '.hidden.wav')
        make_wav(tmp_path / 'downloads' / UUID / 'Someone - Visitor Song.wav')
        make_wav(tmp_path / '.trash' / 'Gone - Song.wav')
        await login(client)
        playlists = (await (await client.get('/api/library')).json())['playlists']
        names = [p['name'] for p in playlists]
        assert 'downloads' not in names and '.trash' not in names and 'data' not in names   # tmp_path/data is Song-DL's own
        assert {'Road Trip', 'Empty (DJ)'} <= set(names)
        trip = next(p for p in playlists if p['name'] == 'Road Trip')
        assert trip['songs'] == 2 and trip['size'] > 0

        songs = (await (await client.get('/api/library/songs', params={'playlist': 'Road Trip'})).json())['songs']
        assert [(s['artist'], s['title']) for s in songs] == [('Daft Punk', 'Digital Love'), ('Justice', 'Genesis')]
        assert songs[0]['format'] == 'WAV'
        assert songs[0]['bitrate'] == 1411
        assert songs[0]['sampleRate'] == 44100 and songs[0]['bitDepth'] == 16
        assert songs[0]['path'] == 'Daft Punk - Digital Love.wav'

        res = await client.get('/api/library/songs', params={'playlist': 'downloads'})
        assert res.status == 404
    finally:
        await client.close()


async def test_a_song_is_downloaded_and_deleted(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path / 'music')
    try:
        song = tmp_path / 'music' / 'Mix' / 'disc 1' / 'Daft Punk - Digital Love.wav'
        make_wav(song)
        make_wav(tmp_path / 'music' / 'Other' / 'Secret - Song.wav')
        await login(client)
        await client.engine.library.refresh()
        assert str(song) in client.engine.library.songs

        q = {'playlist': 'Mix', 'path': 'disc 1/Daft Punk - Digital Love.wav'}
        res = await client.get('/api/library/file', params=q)
        assert res.status == 200
        assert 'attachment' in res.headers['Content-Disposition']
        assert await res.read() == song.read_bytes()

        # Only songs in that playlist
        for path in ('../Other/Secret - Song.wav', '/etc/passwd', '', 'disc 1'):
            res = await client.get('/api/library/file', params={'playlist': 'Mix', 'path': path})
            assert res.status == 404, path
        res = await client.delete('/api/library/song', params={'playlist': 'Mix', 'path': '../Other/Secret - Song.wav'})
        assert res.status == 404
        assert (tmp_path / 'music' / 'Other' / 'Secret - Song.wav').exists()

        res = await client.delete('/api/library/song', params=q)
        assert res.status == 200
        assert not song.exists()
        assert not song.parent.exists()            # the emptied sub-folder goes
        assert (tmp_path / 'music' / 'Mix').is_dir()   # the playlist stays
        assert str(song) not in client.engine.library.songs
        songs = (await (await client.get('/api/library/songs', params={'playlist': 'Mix'})).json())['songs']
        assert songs == []
        assert (await client.get('/api/library/file', params=q)).status == 404
    finally:
        await client.close()


async def test_without_a_server_folder_the_visitors_folders_are_left_out(tmp_path, fake):
    client = await start_site(tmp_path, fake)   # admins' playlists go in the downloads folder too
    try:
        make_wav(tmp_path / 'downloads' / UUID / 'Someone - Visitor Song.wav')
        make_wav(tmp_path / 'downloads' / 'Party' / 'Daft Punk - One More Time.wav')
        await login(client)
        playlists = (await (await client.get('/api/library')).json())['playlists']
        assert [p['name'] for p in playlists] == ['Party']
    finally:
        await client.close()


async def test_a_playlist_is_deleted_with_its_songs(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path)   # music/downloads, as on the real server
    try:
        music = tmp_path
        song = music / 'Mix' / 'disc 1' / 'Daft Punk - Digital Love.wav'
        make_wav(song)
        make_wav(music / 'Mix' / 'Justice - Genesis.wav')
        (music / 'Mix' / 'cover.jpg').write_bytes(b'x')
        make_wav(music / 'Mix (DJ)' / 'Justice - Genesis.wav')
        make_wav(music / 'downloads' / UUID / 'Someone - Visitor Song.wav')
        assert (await client.delete('/api/library/playlist', params={'playlist': 'Mix'})).status == 401
        await login(client)
        await client.engine.library.refresh()
        assert str(song) in client.engine.library.songs
        await client.get('/api/library/songs', params={'playlist': 'Mix'})   # fills the cache

        # Only playlists: not the visitors' downloads, nor anything outside
        for name in ('downloads', 'data', '..', '', 'Nope', 'Mix/disc 1'):
            res = await client.delete('/api/library/playlist', params={'playlist': name})
            assert res.status == 404, name
        assert (music / 'downloads' / UUID).is_dir()

        res = await client.delete('/api/library/playlist', params={'playlist': 'Mix'})
        assert res.status == 200, await res.text()
        assert not (music / 'Mix').exists()
        assert (music / 'Mix (DJ)' / 'Justice - Genesis.wav').exists()   # its DJ copies are a playlist of their own
        assert not any(k.startswith(str(music / 'Mix') + '/') for k in client.engine.library.songs)
        names = [p['name'] for p in (await (await client.get('/api/library')).json())['playlists']]
        assert names == ['Mix (DJ)']
        assert (await client.get('/api/library/songs', params={'playlist': 'Mix'})).status == 404
        assert (await client.delete('/api/library/playlist', params={'playlist': 'Mix'})).status == 404
    finally:
        await client.close()


async def test_a_playlist_still_downloading_is_not_deleted(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path / 'music')
    try:
        make_wav(tmp_path / 'music' / 'Mix' / 'Justice - Genesis.wav')
        await login(client)
        from songdl.server_queue import QueueItem
        item = QueueItem('x', [], 'Digital Love', 'Daft Punk', '', 'wav', 'Mix/disc 1', 'admin')
        item.status = 'paused'
        client.queue.items[item.id] = item
        res = await client.delete('/api/library/playlist', params={'playlist': 'Mix'})
        assert res.status == 409
        assert 'Downloads page' in (await res.json())['error']
        assert (tmp_path / 'music' / 'Mix' / 'Justice - Genesis.wav').exists()

        item.status = 'cancelled'
        res = await client.delete('/api/library/playlist', params={'playlist': 'Mix'})
        assert res.status == 200
        assert not (tmp_path / 'music' / 'Mix').exists()
    finally:
        await client.close()


async def test_a_playlist_is_downloaded_as_a_zip(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path / 'music')
    try:
        make_wav(tmp_path / 'music' / 'Mix' / 'Justice - Genesis.wav')
        make_wav(tmp_path / 'music' / 'Mix' / 'disc 2' / 'Daft Punk - Digital Love.wav')
        make_wav(tmp_path / 'music' / 'Other' / 'Secret - Song.wav')
        (tmp_path / 'music' / 'Mix' / 'Secret.wav').symlink_to(tmp_path / 'music' / 'Other' / 'Secret - Song.wav')
        (tmp_path / 'music' / 'Empty').mkdir()
        assert (await client.get('/api/library/zip', params={'playlist': 'Mix'})).status == 401
        await login(client)

        res = await client.get('/api/library/zip', params={'playlist': 'Mix'})
        assert res.status == 200 and "filename*=UTF-8''Mix.zip" in res.headers['Content-Disposition']
        zf = zipfile.ZipFile(io.BytesIO(await res.read()))
        # Sub-folders kept; the symlink out of the playlist left out
        assert zf.namelist() == ['disc 2/Daft Punk - Digital Love.wav', 'Justice - Genesis.wav']
        assert zf.read('Justice - Genesis.wav') == (tmp_path / 'music' / 'Mix' / 'Justice - Genesis.wav').read_bytes()

        assert (await client.get('/api/library/zip', params={'playlist': 'Empty'})).status == 404
        assert (await client.get('/api/library/zip', params={'playlist': '../music'})).status == 404
    finally:
        await client.close()
