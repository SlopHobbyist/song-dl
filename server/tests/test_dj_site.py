"""DJ mode through the site: browser downloads, zips, the server queue and imports. The fake
Soulseek's files aren't real audio, so the conversion itself is stood in for here (test_dj.py
runs the real one)."""

import io
import zipfile

import pytest

from songdl import dj

from tests.test_site import fetch, finished, queue_view, search, wait_for, zip_url


@pytest.fixture
def converted(monkeypatch):
    """Stands in for dj.convert: writes "<stem>.wav" and remembers what it was asked."""
    calls = []

    def fake_convert(src, folder, stem, title='', artist='', album=''):
        calls.append(src.name)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / (stem + '.wav')
        out.write_bytes(b'RIFF\0\0\0\0WAVE' + b'\0' * 1000)
        return dj.Copy(out, 'WAV 24-bit / 48 kHz', True)

    monkeypatch.setattr(dj, 'convert', fake_convert)
    return calls


async def test_a_dj_download_hands_over_the_cdj_copy_and_converts_once(site, converted):
    job = await search(site, 'daft punk digital love', ['flac'])
    result = job['results'][0]
    assert result['dj'].startswith('WAV')   # what the card says DJ mode will make
    res = await site.post('/api/download', json={'result': result['id'], 'dj': True})
    one = (await res.json())['id']
    view = (await finished(site, [one]))[one]
    assert view['state'] == 'done', view
    assert view['name'] == 'Daft Punk - Digital Love.wav' and view['dj'] == 'WAV 24-bit / 48 kHz'
    assert view['size'] == 1012 and 'WAV 24-bit / 48 kHz' in view['note']
    body = await (await site.get(view['file'])).read()
    assert body[:4] == b'RIFF'

    # The same song again: the copy made before
    two = (await (await site.post('/api/download', json={'result': result['id'], 'dj': True})).json())['id']
    assert (await finished(site, [two]))[two]['state'] == 'done'
    assert len(converted) == 1
    names = zipfile.ZipFile(io.BytesIO(await (await site.get(await zip_url(site, [one], 'Set'))).read())).namelist()
    assert names == ['Daft Punk - Digital Love.wav']

    # Without DJ mode it's the original, and a search never finds the CDJ copy
    plain = await fetch(site, (await search(site, 'daft punk digital love', ['wav', 'flac']))['results'][0]['id'])
    assert (await finished(site, [plain]))[plain]['name'].endswith('.flac')


async def test_a_failed_conversion_fails_the_download(site, monkeypatch):
    def broken(*args):
        raise dj.UserError(dj.FAILED)

    monkeypatch.setattr(dj, 'convert', broken)
    job = await search(site, 'daft punk digital love', ['flac'])
    jid = (await (await site.post('/api/download', json={'result': job['results'][0]['id'], 'dj': True})).json())['id']
    view = (await finished(site, [jid]))[jid]
    assert view['state'] == 'failed' and view['error'] == dj.FAILED


async def test_the_server_saves_cdj_copies_in_a_dj_folder(admin, converted, tmp_path):
    job = await search(admin, 'daft punk digital love', ['flac'])
    res = await admin.post('/api/queue', json={'playlist': 'Road Trip', 'dj': True, 'results': [job['results'][0]['id']]})
    assert (await res.json())['folder'] == 'Road Trip (DJ)'
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] in ('done', 'failed'))
    item = view['items'][0]
    assert item['status'] == 'done' and item['dj'] and 'WAV 24-bit / 48 kHz' in item['note'], item
    folder = tmp_path / 'downloads' / 'Road Trip (DJ)'
    assert [p.name for p in folder.iterdir()] == ['Daft Punk - Digital Love.wav']

    # Again: it's already there, so nothing is downloaded or converted
    await admin.post('/api/queue/clear-done')
    await admin.post('/api/queue', json={'playlist': 'Road Trip', 'dj': True, 'results': [job['results'][0]['id']]})
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] == 'done')
    assert 'Already in the folder' in view['items'][0]['note'] and len(converted) == 1

    # A plain save of the same playlist goes in its own folder
    res = await admin.post('/api/queue', json={'playlist': 'Road Trip', 'results': [job['results'][0]['id']]})
    assert (await res.json())['folder'] == 'Road Trip'


async def test_a_dj_import_saves_cdj_copies(admin, converted, tmp_path):
    res = await admin.post('/api/imports', json={
        'csv': 'title,artist\nDigital Love,Daft Punk\n', 'name': 'Set.csv', 'formats': ['flac'],
        'playlist': 'Set', 'dj': True})
    imp = await res.json()
    assert imp['dj'] and imp['folder'] == 'Set (DJ)/'
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] == 'done')
    assert view['items'][0]['dj']
    assert [p.name for p in (tmp_path / 'downloads' / 'Set (DJ)').iterdir()] == ['Daft Punk - Digital Love.wav']


async def test_the_session_says_whether_dj_mode_works(site):
    assert (await (await site.get('/api/session')).json())['djMode'] == dj.available()
