"""The whole backend against the fake slskd."""

import asyncio
import io
import json
import zipfile

from tests.conftest import ADMIN, start_site


async def wait_for(poll, done, timeout=15):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await poll()
        if done(value):
            return value
        assert asyncio.get_running_loop().time() < deadline, f'timed out; last: {value}'
        await asyncio.sleep(0.1)


async def search(client, query, formats):
    res = await client.post('/api/search', json={'query': query, 'formats': formats})
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']

    async def poll():
        return await (await client.get(f'/api/search/{sid}')).json()

    return await wait_for(poll, lambda v: v['state'] != 'running')


async def scan(client, csv, formats):
    res = await client.post('/api/scan', params={'formats': formats, 'name': 'test.csv'}, data=csv.encode())
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']

    async def poll():
        return await (await client.get(f'/api/scan/{sid}')).json()

    return await wait_for(poll, lambda v: v['state'] != 'running')


async def fetch(client, result_id):
    res = await client.post('/api/download', json={'result': result_id})
    assert res.status == 200, await res.text()
    return (await res.json())['id']


async def downloads(client, ids):
    res = await client.get('/api/downloads', params=[('id', i) for i in ids])
    return (await res.json())['jobs']


async def finished(client, ids):
    return await wait_for(lambda: downloads(client, ids), lambda jobs: all(j['state'] != 'running' for j in jobs.values()))


async def queue_view(client):
    return await (await client.get('/api/queue')).json()


async def test_search_finds_the_best_copy_of_each_format(site):
    job = await search(site, 'daft punk digital love', ['wav', 'aiff', 'flac', 'mp3'])
    assert job['state'] == 'done' and job['missing'] == []
    assert [r['format'] for r in job['results']] == ['WAV', 'AIFF', 'FLAC', 'MP3']
    assert (job['title'], job['artist'], job['album']) == ('Digital Love', 'Daft Punk', 'Discovery')
    flac = job['results'][2]
    assert flac['quality'] == '24-bit / 96 kHz' and flac['filename'] == 'Daft Punk - Digital Love.flac'
    assert job['results'][3]['quality'] == '320 kbps'


async def test_search_reports_missing_formats_and_unknown_songs(site):
    job = await search(site, 'radiohead weird fishes', ['flac', 'wav'])
    assert [r['format'] for r in job['results']] == ['FLAC'] and job['missing'] == ['WAV']
    job = await search(site, 'nobody never heard', ['flac'])
    assert job['state'] == 'done' and job['results'] == [] and job['missing'] == ['FLAC']


async def test_search_input_is_checked(site):
    for body in ({'query': 'ab', 'formats': ['flac']}, {'query': 'daft punk', 'formats': []},
                 {'query': 'daft punk', 'formats': ['ogg']}, {'query': 'x' * 300, 'formats': ['flac']}):
        res = await site.post('/api/search', json=body)
        assert res.status == 400 and 'error' in await res.json()


async def test_download_to_the_browser_and_zip(site, fake):
    job = await search(site, 'daft punk digital love', ['wav', 'flac'])
    ids = [await fetch(site, r['id']) for r in job['results']]
    jobs = await finished(site, ids)
    assert all(j['state'] == 'done' and j['progress'] == 1 for j in jobs.values())

    one = jobs[ids[1]]
    assert one['name'] == 'Daft Punk - Digital Love.flac'
    res = await site.get(one['file'])
    assert res.status == 200
    assert 'Daft%20Punk%20-%20Digital%20Love.flac' in res.headers['Content-Disposition']
    body = await res.read()
    assert body[:4] == b'fLaC' and len(body) == one['size']

    res = await site.get('/api/zip', params=[('job', i) for i in ids] + [('name', 'My songs')])
    assert res.status == 200 and 'My%20songs.zip' in res.headers['Content-Disposition']
    names = zipfile.ZipFile(io.BytesIO(await res.read())).namelist()
    assert sorted(names) == ['Daft Punk - Digital Love.flac', 'Daft Punk - Digital Love.wav']


async def test_files_are_only_served_from_fetch_folders(site, fake, tmp_path):
    (tmp_path / 'downloads' / 'private').mkdir(parents=True)
    (tmp_path / 'downloads' / 'private' / 'secret.flac').write_bytes(b'fLaC')
    for path in ('/files/private/secret.flac', '/files/..%2Fx/y', '/files/1b4e28ba-2fa1-11d2-883f-0016d3cca427/none.flac'):
        assert (await site.get(path)).status == 404


async def test_playlist_scan_then_download(site):
    csv = 'title,artist\nDigital Love,Daft Punk\nBarbra Streisand,Duck Sauce\nNo Such Song,Nobody\n'
    job = await scan(site, csv, 'wav,flac')
    assert (job['total'], job['scanned'], job['skipped']) == (3, 3, 0)
    states = [t['state'] for t in job['tracks']]
    assert states == ['found', 'found', 'missing']
    first = job['tracks'][0]
    assert [r['format'] for r in first['results']] == ['WAV', 'FLAC'] and first['size'] == sum(r['size'] for r in first['results'])
    assert job['tracks'][2]['results'] == [] and job['tracks'][2]['size'] is None

    ids = [await fetch(site, r['id']) for t in job['tracks'] for r in t['results']]
    jobs = await finished(site, ids)
    assert len(jobs) == 4 and all(j['state'] == 'done' for j in jobs.values())
    res = await site.get('/api/zip', params=[('job', i) for i in ids])
    assert len(zipfile.ZipFile(io.BytesIO(await res.read())).namelist()) == 4


async def test_scan_rejects_a_bad_csv(site):
    res = await site.post('/api/scan', params={'formats': 'flac'}, data=b'foo,bar\n1,2\n')
    assert res.status == 400 and 'song name column' in (await res.json())['error']


async def test_admin_pages_and_queue_need_a_login(site):
    res = await site.get('/server/', allow_redirects=False)
    assert res.status == 302 and res.headers['Location'] == '/login/'
    assert (await site.get('/all/', allow_redirects=False)).status == 302
    for method, path in (('get', '/api/queue'), ('post', '/api/queue'), ('post', '/api/queue/x/pause'),
                         ('delete', '/api/queue/x'), ('post', '/api/queue/clear-done')):
        assert (await getattr(site, method)(path)).status == 401
    assert (await site.get('/')).status == 200
    assert (await site.get('/login/')).status == 200


async def test_pages_live_at_folder_addresses(site):
    for path, target in (('/index.html', '/'), ('/login', '/login/'), ('/login.html', '/login/'),
                         ('/login/index.html', '/login/'), ('/all.html', '/all/'), ('/server?x=1', '/server/?x=1')):
        res = await site.get(path, allow_redirects=False)
        assert res.status == 301 and res.headers['Location'] == target, path
    assert (await site.get('/login/index.htm')).status == 404


async def test_only_the_site_files_are_served(site):
    for path in ('/vendored/web-design-wavefront/.git/config', '/vendored/web-design-wavefront/TODO.txt',
                 '/README.md', '/../config.yaml', '/vendored/web-design-wavefront/assets/fonts/../../.gitignore'):
        assert (await site.get(path)).status == 404, path
    for path in ('/style.css', '/main.js', '/vendored/web-design-wavefront/design_language.css',
                 '/vendored/web-design-wavefront/assets/fonts/MaterialIcons.woff2'):
        assert (await site.get(path)).status == 200, path
    csp = (await site.get('/')).headers['Content-Security-Policy']
    assert "frame-ancestors 'none'" in csp and 'cloudflare' not in csp


async def test_login(site):
    res = await site.post('/api/login', json={'username': 'admin', 'password': 'wrong-password'})
    assert res.status == 401
    res = await site.post('/api/login', json={'username': 'nobody', 'password': ADMIN['admin']})
    assert res.status == 401
    assert (await (await site.get('/api/session')).json())['user'] is None

    res = await site.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
    cookie = res.headers['Set-Cookie']
    assert 'HttpOnly' in cookie and 'SameSite=Strict' in cookie and 'Max-Age' not in cookie   # not remembered
    assert (await (await site.get('/api/session')).json())['user'] == 'admin'
    assert (await site.get('/server/', allow_redirects=False)).status == 200

    await site.post('/api/logout')
    assert (await (await site.get('/api/session')).json())['user'] is None

    res = await site.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin'], 'remember': True})
    assert 'Max-Age=2592000' in res.headers['Set-Cookie']


async def test_login_is_off_without_admins(tmp_path, fake):
    client = await start_site(tmp_path, fake, admins={})
    try:
        res = await client.post('/api/login', json={'username': 'admin', 'password': 'whatever-it-is'})
        assert res.status == 403
        assert (await client.get('/server/', allow_redirects=False)).status == 302
    finally:
        await client.close()


async def test_repeated_wrong_passwords_lock_the_ip_out(site):
    for _ in range(5):
        await site.post('/api/login', json={'username': 'admin', 'password': 'wrong-password'})
    res = await site.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
    assert res.status == 429


async def test_queue_saves_songs_into_a_folder(admin, tmp_path):
    job = await search(admin, 'daft punk digital love', ['wav', 'flac'])
    res = await admin.post('/api/queue', json={'folder': 'road trip/2026', 'results': [r['id'] for r in job['results']]})
    assert await res.json() == {'queued': 2, 'skipped': 0}

    # Queueing the same songs again while they are in the queue does nothing
    res = await admin.post('/api/queue', json={'folder': 'road trip/2026', 'results': [r['id'] for r in job['results']]})
    assert await res.json() == {'queued': 0, 'skipped': 2}

    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and all(i['status'] == 'done' for i in v['items']))
    assert view['me'] == 'admin' and view['active'] == 0
    assert {i['format'] for i in view['items']} == {'WAV', 'FLAC'}
    assert all(i['by'] == 'admin' and i['folder'] == 'road trip/2026/' for i in view['items'])

    folder = tmp_path / 'downloads' / 'road trip' / '2026'
    assert sorted(p.name for p in folder.iterdir()) == ['Daft Punk - Digital Love.flac', 'Daft Punk - Digital Love.wav']
    # The staging copies are gone once the songs are in place
    assert [p.name for p in (tmp_path / 'downloads').iterdir()] == ['road trip']

    # Asking again finds the files already there
    res = await admin.post('/api/queue/clear-done')
    assert (await res.json())['cleared'] == 2
    await admin.post('/api/queue', json={'folder': 'road trip/2026', 'results': [job['results'][0]['id']]})
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] == 'done')
    assert 'Already in the folder' in view['items'][0]['note']
    assert len(list(folder.iterdir())) == 2


async def test_queue_controls(admin, fake, tmp_path):
    fake.speed = 0.5   # downloads take 8 seconds, so there is time to pause them
    job = await search(admin, 'daft punk digital love', ['wav', 'flac', 'aiff', 'mp3'])
    await admin.post('/api/queue', json={'folder': 'controls', 'results': [r['id'] for r in job['results']]})

    view = await wait_for(lambda: queue_view(admin), lambda v: sum(i['status'] == 'downloading' for i in v['items']) == 3)
    by_status = {i['status']: i for i in view['items']}
    assert set(by_status) == {'downloading', 'queued'}          # three at a time
    assert view['active'] == 4

    running = next(i for i in view['items'] if i['status'] == 'downloading')
    assert (await admin.post(f"/api/queue/{running['id']}/pause")).status == 200
    view = await queue_view(admin)
    assert next(i for i in view['items'] if i['id'] == running['id'])['status'] == 'paused'
    # The freed slot goes to the waiting song
    view = await wait_for(lambda: queue_view(admin), lambda v: sum(i['status'] == 'downloading' for i in v['items']) == 3)

    other = next(i for i in view['items'] if i['status'] == 'downloading')
    await admin.post(f"/api/queue/{other['id']}/cancel")
    victim = next(i for i in (await queue_view(admin))['items'] if i['status'] == 'downloading')
    assert (await admin.delete(f"/api/queue/{victim['id']}")).status == 200
    assert victim['id'] not in [i['id'] for i in (await queue_view(admin))['items']]

    await admin.post(f"/api/queue/{running['id']}/resume")
    await admin.post(f"/api/queue/{other['id']}/resume")
    fake.speed = 20
    view = await wait_for(lambda: queue_view(admin), lambda v: all(i['status'] == 'done' for i in v['items']), timeout=30)
    assert len(view['items']) == 3
    assert (await admin.post('/api/queue/nope/pause')).status == 404


async def test_pause_all(admin, fake):
    fake.speed = 0.5
    job = await search(admin, 'daft punk digital love', ['wav', 'flac'])
    await admin.post('/api/queue', json={'folder': 'p', 'results': [r['id'] for r in job['results']]})
    await wait_for(lambda: queue_view(admin), lambda v: any(i['status'] == 'downloading' for i in v['items']))
    assert (await (await admin.post('/api/queue/pause-all')).json())['paused'] == 2
    view = await queue_view(admin)
    assert [i['status'] for i in view['items']] == ['paused', 'paused'] and view['active'] == 0


async def test_queue_folder_cannot_leave_the_downloads_folder(admin):
    job = await search(admin, 'daft punk digital love', ['flac'])
    for folder in ('..', '../escape', 'a/../../b', 'C:/x'):
        res = await admin.post('/api/queue', json={'folder': folder, 'results': [job['results'][0]['id']]})
        assert res.status == 400, folder


async def test_queue_survives_a_restart(admin, fake, tmp_path):
    fake.speed = 0.2
    job = await search(admin, 'daft punk digital love', ['flac', 'wav'])
    await admin.post('/api/queue', json={'folder': 'kept', 'results': [r['id'] for r in job['results']]})
    await wait_for(lambda: queue_view(admin), lambda v: any(i['status'] == 'downloading' for i in v['items']))
    await admin.close()   # the app shuts down mid-download; the queue is saved

    saved = json.loads((tmp_path / 'data' / 'queue.json').read_text())
    assert len(saved['items']) == 2 and saved['items'][0]['cands']

    fake.speed = 20
    again = await start_site(tmp_path, fake)
    try:
        await again.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
        view = await wait_for(lambda: queue_view(again), lambda v: len(v['items']) == 2 and all(i['status'] == 'done' for i in v['items']))
        assert {i['format'] for i in view['items']} == {'FLAC', 'WAV'}
        assert len(list((tmp_path / 'downloads' / 'kept').iterdir())) == 2
    finally:
        await again.close()


async def test_download_of_an_expired_result_is_refused(site):
    res = await site.post('/api/download', json={'result': 'nope'})
    assert res.status == 404


async def test_all_formats_into_a_new_nested_folder(admin, tmp_path):
    # Several songs start at once and all create the same new folders
    job = await search(admin, 'daft punk digital love', ['wav', 'aiff', 'flac', 'mp3'])
    ids = [r['id'] for r in job['results']]
    await admin.post('/api/queue', json={'folder': 'archive/vinyl-rips', 'results': ids})
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and all(i['status'] in ('done', 'failed') for i in v['items']))
    assert [i['status'] for i in view['items']] == ['done'] * 4, [i['note'] for i in view['items']]
    assert len(list((tmp_path / 'downloads' / 'archive' / 'vinyl-rips').iterdir())) == 4


async def test_queue_saves_into_a_separate_server_folder(tmp_path, fake):
    client = await start_site(tmp_path, fake, server_dir=tmp_path / 'music')
    try:
        assert (await (await client.get('/api/session')).json())['serverFolder'] == 'music'
        res = await client.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})
        assert res.status == 200
        job = await search(client, 'daft punk digital love', ['flac'])
        await client.post('/api/queue', json={'folder': 'mix', 'results': [job['results'][0]['id']]})
        view = await wait_for(lambda: queue_view(client), lambda v: v['items'] and v['items'][0]['status'] in ('done', 'failed'))
        assert view['items'][0]['status'] == 'done', view['items'][0]['note']
        assert [p.name for p in (tmp_path / 'music' / 'mix').iterdir()] == ['Daft Punk - Digital Love.flac']
        # Only the staging copy was in the downloads folder, and it's gone
        assert list((tmp_path / 'downloads').iterdir()) == []
    finally:
        await client.close()
