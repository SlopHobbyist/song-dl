"""The search rate limit, admins' playlist imports, and the lighter queue views."""

import asyncio
import time

from songdl import engine, imports
from songdl.engine import IMPORT, INTERACTIVE, SearchLimiter
from songdl.imports import Importer
from songdl.playlist import Track
from songdl.web import IMPORTER

from tests.test_site import queue_view, wait_for

CSV = 'title,artist\nDigital Love,Daft Punk\nBarbra Streisand,Duck Sauce\nNo Such Song,Nobody\n'


async def test_searches_are_held_to_the_limit():
    limiter = SearchLimiter(2, 0.5)
    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    assert time.monotonic() - started >= 0.45   # the third waited for the first to leave the window


async def test_a_visitors_search_goes_ahead_of_an_import():
    limiter = SearchLimiter(1, 0.4)
    await limiter.acquire()   # the window is full
    order = []

    async def search(priority, name):
        await limiter.acquire(priority)
        order.append(name)

    background = asyncio.create_task(search(IMPORT, 'import'))
    await asyncio.sleep(0.05)
    visitor = asyncio.create_task(search(INTERACTIVE, 'visitor'))
    await asyncio.gather(background, visitor)
    assert order == ['visitor', 'import']


async def start_import(client, csv=CSV, formats='wav,flac', folder='Road Trip', name='road trip.csv'):
    res = await client.post('/api/imports', json={'csv': csv, 'name': name, 'formats': formats, 'folder': folder})
    assert res.status == 200, await res.text()
    return (await res.json())['id']


async def import_view(client, iid):
    imps = (await (await client.get('/api/imports')).json())['imports']
    return next(i for i in imps if i['id'] == iid)


async def test_an_import_queues_every_song_it_finds(admin):
    iid = await start_import(admin)
    view = await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done')
    assert (view['name'], view['total'], view['found'], view['missing'], view['left']) == ('road trip', 3, 2, 1, 0)
    assert view['folder'] == 'Road Trip/' and view['formats'] == ['WAV', 'FLAC']

    items = (await queue_view(admin))['items']
    assert len(items) == view['queued'] >= 2
    assert {i['folder'] for i in items} == {'Road Trip/'}
    assert {i['title'] for i in items} == {'Digital Love', 'Barbra Streisand'}

    res = await admin.get(f'/api/imports/{iid}/missing.csv')
    assert res.status == 200 and 'not%20found.csv' in res.headers['Content-Disposition']
    text = await res.text()
    rows = text.lstrip('\ufeff').splitlines()
    assert rows[1:] == ['No Such Song,Nobody,,,WAV FLAC']

    # The songs it queued download on their own
    await wait_for(lambda: queue_view(admin), lambda q: all(i['status'] == 'done' for i in q['items']), timeout=30)


async def test_imports_take_far_more_than_a_scan(admin, site):
    csv = 'title,artist\n' + ''.join(f'Song {i},Artist {i}\n' for i in range(1500))
    res = await site.post('/api/scan', params={'formats': 'wav', 'name': 'big.csv'}, data=csv.encode())
    assert res.status == 400 and 'up to 300' in (await res.json())['error']

    iid = await start_import(admin, csv, 'wav', '')
    res = await admin.post(f'/api/imports/{iid}/pause')
    assert res.status == 200
    view = await import_view(admin, iid)
    assert view['total'] == 1500 and view['state'] == 'paused' and view['eta'] is None
    assert (await admin.delete(f'/api/imports/{iid}')).status == 200
    assert not (await (await admin.get('/api/imports')).json())['imports']


async def test_pause_and_resume(admin, fake):
    fake.search_seconds = 0.6
    iid = await start_import(admin)
    await admin.post(f'/api/imports/{iid}/pause')
    await asyncio.sleep(1.5)
    view = await import_view(admin, iid)
    assert view['state'] == 'paused' and view['left'] == 3 and view['current'] == []

    await admin.post(f'/api/imports/{iid}/resume')
    view = await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done', timeout=30)
    assert view['found'] == 2


async def test_imports_need_a_login(site):
    res = await site.post('/api/imports', json={'csv': CSV, 'formats': 'wav'})
    assert res.status == 401
    assert (await site.get('/api/imports')).status == 401


async def test_imports_survive_a_restart(admin):
    importer = admin.server.app[IMPORTER]
    imp = importer.add('Saved', ['wav'], 'x', 'admin', [Track('A', 'B'), Track('C', 'D', 'E', 200)])
    importer.pause(imp)
    imp.tracks[0].state, imp.tracks[0].found = 'found', ['wav']
    imp.tracks[1].state = 'searching'   # cut off mid-search
    importer.dirty = True
    importer.save()

    again = Importer(admin.cfg, admin.engine, admin.queue)
    again.load()
    back = again.get(imp.id)
    assert (back.name, back.state, back.folder, back.formats) == ('Saved', 'paused', 'x', ['wav'])
    assert [(t.state, t.found) for t in back.tracks] == [('found', ['wav']), ('pending', [])]
    assert back.tracks[1].track.duration == 200


async def test_the_menu_and_server_page_ask_for_less(admin):
    iid = await start_import(admin, folder='a')
    await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done')
    summary = await (await admin.get('/api/queue', params={'summary': '1'})).json()
    assert set(summary) == {'active', 'speed'}
    mine = await (await admin.get('/api/queue', params={'mine': '1'})).json()
    assert mine['me'] == 'admin' and len(mine['items']) == len((await queue_view(admin))['items'])


def test_time_left_uses_the_search_limit_until_it_has_a_pace():
    imp = imports.Import('x', ['wav', 'aiff'], '', 'a', [])
    expected = 10 * 2 * imports.SEARCHES_PER_SONG * imports.SEARCH_WINDOW / imports.SEARCHES_PER_WINDOW
    assert imp.eta(10) == round(expected)
    imp.run_started, imp.run_done = time.monotonic() - 100, 10
    assert imp.eta(10) == 100
