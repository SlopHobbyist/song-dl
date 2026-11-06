"""Songs already on the server are handed out again, before any search (library.py)."""

import os
import time
from types import SimpleNamespace

import pytest

from songdl import web
from songdl.library import Library, steps
from tests.test_site import fetch, finished, queue_view, scan, search, wait_for

STAGED = '0b2615b4-9de7-4cf6-8c33-0b89e57cee04'


@pytest.fixture
def lib(tmp_path):
    cfg = SimpleNamespace(downloads_dir=tmp_path / 'downloads', server_folder=tmp_path / 'downloads',
                          data_dir=tmp_path / 'data')
    cfg.downloads_dir.mkdir()
    return Library(cfg)


def song(lib, rel, title, artist, fmt='flac', origin='soulseek', name=None, length=None, data=b'fLaC'):
    path = lib.cfg.downloads_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + bytes(250_000))
    lib._put({'path': str(path), 'title': title, 'artist': artist, 'album': '', 'fmt': fmt,
              'size': path.stat().st_size, 'length': length, 'bit_depth': 16, 'sample_rate': 44100,
              'bitrate': None, 'origin': origin, 'name': name or path.name, 'from': None, 'link': None,
              'query': '', 'added': time.time()})
    return path


def found(lib, query, order, duration=None):
    return [os.path.basename(c.filename) for c in lib.find(query, order, duration)]


def test_a_copy_counts_when_the_query_names_the_song(lib):
    song(lib, f'{STAGED}/1-03 Digital Love.flac', 'Digital Love', 'Daft Punk')
    order = steps(['wav', 'flac'], ['soulseek'])
    assert found(lib, 'daft punk digital love', order) == ['1-03 Digital Love.flac']
    assert found(lib, 'Digital Love - Daft Punk', order) == ['1-03 Digital Love.flac']
    assert found(lib, 'digital love', order) == ['1-03 Digital Love.flac']
    assert found(lib, 'daft punk', order) == []                      # just the artist: search for real
    assert found(lib, 'daft punk digital love live', order) == []    # asks for something it isn't
    assert found(lib, 'daft punk digital love', ['wav']) == []        # not a format the visitor wants
    assert found(lib, 'daft punk digital love', ['youtube']) == []


def test_featured_artists_and_ids_dont_count_against_a_title(lib):
    song(lib, 'mine/Blessings.wav', 'Blessings (feat. Clementine Douglas) [0mdYVvWJemI]', 'Calvin Harris', fmt='wav')
    assert found(lib, 'calvin harris blessings', ['wav']) == ['Blessings.wav']


def test_a_remix_only_counts_when_asked_for(lib):
    song(lib, f'{STAGED}/x.wav', 'Big Love', 'Pete Heller', fmt='wav', name='Pete Heller - Big Love (The Dronez Mix).wav')
    assert found(lib, 'pete heller big love', ['wav']) == []
    assert found(lib, 'pete heller big love dronez mix', ['wav']) == ['x.wav']


def test_the_visitors_order_picks_between_copies(lib):
    song(lib, 'a/Digital Love.flac', 'Digital Love', 'Daft Punk')
    song(lib, 'b/Digital Love.opus', 'Digital Love', 'Daft Punk', fmt='opus', origin='youtube', data=b'OggS')
    assert found(lib, 'daft punk digital love', steps(['wav', 'flac'], ['soulseek', 'youtube'])) == ['Digital Love.flac']
    assert found(lib, 'daft punk digital love', steps(['wav', 'flac'], ['youtube', 'soulseek'])) == ['Digital Love.opus']
    assert found(lib, 'daft punk digital love', steps(['wav'], ['soulseek', 'soundcloud'])) == []


def test_a_playlists_length_has_to_agree(lib):
    song(lib, 'a/One More Time.flac', 'One More Time', 'Daft Punk', length=320)
    assert found(lib, 'daft punk one more time', ['flac'], duration=240) == []
    assert found(lib, 'daft punk one more time', ['flac'], duration=325) == ['One More Time.flac']


def test_a_file_that_is_gone_doesnt_count(lib):
    path = song(lib, 'a/Digital Love.flac', 'Digital Love', 'Daft Punk')
    path.unlink()
    assert found(lib, 'daft punk digital love', ['flac']) == []


async def test_the_folders_are_looked_through(lib):
    root = lib.cfg.downloads_dir
    # Put there some other way: known by its name ("Artist - Title"), as it has no tags
    (root / 'old stuff').mkdir()
    (root / 'old stuff' / 'Daft Punk - One More Time.flac').write_bytes(b'fLaC' + bytes(250_000))
    (root / 'old stuff' / 'cover.jpg').write_bytes(bytes(250_000))
    # A download written down, and a second link to it, as made when it's handed out again
    first = song(lib, f'{STAGED}/03 - Digital Love.flac', 'Digital Love', 'Daft Punk')
    (root / '1b4e28ba-2fa1-11d2-883f-0016d3cca427').mkdir()
    again = root / '1b4e28ba-2fa1-11d2-883f-0016d3cca427' / first.name
    os.link(first, again)

    await lib.refresh()
    assert sorted(lib.songs) == sorted(map(str, [first, again, root / 'old stuff' / 'Daft Punk - One More Time.flac']))
    assert found(lib, 'daft punk one more time', ['flac']) == ['Daft Punk - One More Time.flac']
    assert lib.songs[str(again)]['title'] == 'Digital Love'   # the link is the same song

    # Once the first copy is deleted, the second one still counts
    first.unlink()
    await lib.refresh()
    assert str(first) not in lib.songs
    assert found(lib, 'daft punk digital love', ['flac']) == ['03 - Digital Love.flac']

    # ...and it's all remembered
    lib.save()
    again_lib = Library(lib.cfg)
    again_lib.load()
    assert set(again_lib.songs) == set(lib.songs)
    assert found(again_lib, 'daft punk digital love', ['flac']) == ['03 - Digital Love.flac']


async def test_a_song_downloaded_before_is_handed_out_again(site, fake, tmp_path):
    job = await search(site, 'daft punk digital love', ['wav'])
    first = await fetch(site, job['results'][0]['id'])
    one = (await finished(site, [first]))[first]
    assert one['state'] == 'done' and job['results'][0]['local'] is False
    searches, transfers = len(fake.searches), len(fake.transfers)

    # The next visitor: no search, no download
    job = await search(site, 'Digital Love Daft Punk', ['wav', 'flac'])
    [result] = job['results']
    assert result['local'] is True and result['format'] == 'WAV' and result['source'] == 'soulseek'
    assert (job['title'], job['artist'], job['missing']) == ('Digital Love', 'Daft Punk', [])
    second = await fetch(site, result['id'])
    two = (await finished(site, [second]))[second]
    assert two['state'] == 'done' and two['name'] == 'Daft Punk - Digital Love.wav'
    assert (len(fake.searches), len(fake.transfers)) == (searches, transfers)

    a, b = await site.get(one['file']), await site.get(two['file'])
    assert a.status == b.status == 200 and await a.read() == await b.read()
    files = [p for p in (tmp_path / 'downloads').rglob('*.wav')]
    assert len(files) == 2 and files[0].stat().st_ino == files[1].stat().st_ino   # a link: no extra space

    # Someone who only wants FLAC still gets a search
    job = await search(site, 'daft punk digital love', ['flac'])
    assert job['results'][0]['local'] is False and len(fake.searches) == searches + 1

    # A playlist scan uses it too, and only searches for the rest
    searches = len(fake.searches)
    job = await scan(site, 'title,artist\nDigital Love,Daft Punk\nWeird Fishes Arpeggi,Radiohead\n', 'wav,flac')
    assert [[(r['format'], r['local']) for r in t['results']] for t in job['tracks']] == \
        [[('WAV', True)], [('FLAC', False)]]
    texts = [f['text'] for f in list(fake.searches.values())[searches:]]
    assert texts and all('weird fishes' in t for t in texts)


async def test_the_queue_takes_a_copy_already_here(admin, fake, tmp_path):
    job = await search(admin, 'daft punk digital love', ['flac'])
    await finished(admin, [await fetch(admin, job['results'][0]['id'])])
    transfers = len(fake.transfers)

    job = await search(admin, 'daft punk digital love', ['flac'])
    assert job['results'][0]['local'] is True
    await admin.post('/api/queue', json={'folder': 'sets', 'results': [job['results'][0]['id']]})
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] == 'done')
    assert 'Saved as' in view['items'][0]['note'] and len(fake.transfers) == transfers
    saved = tmp_path / 'downloads' / 'sets' / 'Daft Punk - Digital Love.flac'
    assert saved.is_file() and str(saved) in admin.engine.library.songs


def test_a_file_handed_out_again_is_kept_longer(tmp_path):
    folder = tmp_path / STAGED
    folder.mkdir()
    path = folder / 'song.flac'
    path.write_bytes(b'fLaC')
    old = time.time() - 30 * 86400
    os.utime(path, (old, old))
    # Its modified time is old, but it was linked just now (which sets its change time)
    os.link(path, tmp_path / 'link.flac')
    assert web.delete_old_downloads(tmp_path, 7) == 0 and path.exists()


async def test_an_import_takes_copies_already_here(admin, fake, tmp_path):
    from tests.test_imports import import_view, start_import
    job = await search(admin, 'daft punk digital love', ['wav'])
    await finished(admin, [await fetch(admin, job['results'][0]['id'])])
    searches, transfers = len(fake.searches), len(fake.transfers)

    iid = await start_import(admin, 'title,artist\nDigital Love,Daft Punk\n')
    view = await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done')
    assert (view['found'], view['queued'], view['elsewhere']) == (1, 1, 0)
    await wait_for(lambda: queue_view(admin), lambda q: q['items'] and q['items'][0]['status'] == 'done')
    assert (len(fake.searches), len(fake.transfers)) == (searches, transfers)
    assert (tmp_path / 'downloads' / 'Road Trip' / 'Daft Punk - Digital Love.wav').is_file()


def test_accents_dont_matter(lib):
    song(lib, 'a/Macarena.wav', 'Macarena', 'Los Del Río', fmt='wav')
    assert found(lib, 'los del rio macarena', ['wav']) == ['Macarena.wav']
