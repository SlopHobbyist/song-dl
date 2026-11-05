"""A stand-in for slskd, for tests and for trying the site without Soulseek.

    python -m tests.fake_slskd --downloads ../downloads --port 5030

It serves the parts of slskd's API that Song-DL uses and a small made-up music
library. Searches take about a second; downloads take a few seconds and write
a file with the right header and size into the downloads folder.
"""

import argparse
import time
import uuid
from pathlib import Path

from aiohttp import web

# artist, title, album, formats on offer (every song is also on offer as an mp3)
SONGS = [
    ('Daft Punk', 'Digital Love', 'Discovery', ('flac', 'wav', 'aiff')),
    ('Daft Punk', 'One More Time', 'Discovery', ('flac', 'wav')),
    ('Radiohead', 'Weird Fishes Arpeggi', 'In Rainbows', ('flac',)),
    ('Duck Sauce', 'Barbra Streisand', 'Barbra Streisand', ('flac', 'wav', 'aiff')),
    ('Duck Sauce', 'aNYway', 'Quack', ('wav',)),
    ('Kavinsky', 'Nightcall', 'OutRun', ('flac', 'aiff')),
]
# Any other song has no copies at all, so "not found" can be seen
FAKE_DEFAULTS = {'flac': dict(bitDepth=16, sampleRate=44100, bitrate=900),
                 'wav': dict(bitDepth=16, sampleRate=44100, bitrate=1411),
                 'aiff': dict(bitDepth=16, sampleRate=44100, bitrate=1411),
                 'mp3': dict(bitDepth=None, sampleRate=44100, bitrate=320)}
HEADERS = {
    'flac': b'fLaC', 'mp3': b'\xff\xfb\x90\x00',
    'wav': b'RIFF\0\0\0\0WAVE', 'aiff': b'FORM\0\0\0\0AIFF',
}
USERS = [('alice', True, 0, 4_000_000), ('bob', False, 3, 900_000)]


def library():
    files = []
    for n, (artist, title, album, formats) in enumerate(SONGS):
        for fmt in (*formats, 'mp3'):
            d = FAKE_DEFAULTS[fmt]
            for u, (user, free, queue, speed) in enumerate(USERS):
                if fmt == 'mp3' and u:
                    continue
                depth = d['bitDepth'] + (8 if (u == 0 and fmt in ('flac', 'wav')) else 0) if d['bitDepth'] else None
                rate = 96000 if depth == 24 else d['sampleRate']
                length = 200 + n * 7
                size = 300_000 + n * 40_000 + len(fmt) * 10_000 + u * 1_000
                files.append(dict(
                    user=user, free=free, queue=queue, speed=speed,
                    filename=f'@@music\\{artist}\\{album}\\{n + 1:02d} - {title}.{fmt}',
                    size=size, length=length, bitDepth=depth, sampleRate=rate, bitRate=d['bitrate'], fmt=fmt))
    return files


class Fake:
    def __init__(self, downloads: Path, speed=1.0, search_seconds=1.2):
        self.downloads = downloads
        self.search_seconds = search_seconds
        self.speed = speed    # 1 = a download takes about 4 seconds; 0.25 is four times as long
        self.files = library()
        self.searches = {}
        self.transfers = {}
        self.logged_in = True

    def responses(self, text):
        words = text.lower().replace('-', ' ').split()
        out = {}
        for f in self.files:
            hay = f['filename'].lower().replace('\\', ' ').replace('.', ' ').replace('-', ' ')
            if all(w in hay.split() for w in words):
                out.setdefault(f['user'], []).append(f)
        res = []
        for user, files in out.items():
            _, free, queue, speed = next(u for u in USERS if u[0] == user)
            res.append({
                'username': user, 'hasFreeUploadSlot': free, 'queueLength': queue, 'uploadSpeed': speed,
                'fileCount': len(files), 'lockedFileCount': 0,
                'files': [{'filename': f['filename'], 'size': f['size'], 'length': f['length'],
                           'bitDepth': f['bitDepth'], 'sampleRate': f['sampleRate'], 'bitRate': f['bitRate'],
                           'isVariableBitRate': False, 'isLocked': False} for f in files],
            })
        return res

    def transfer_state(self, t):
        age = time.monotonic() - t['created']
        if t.get('cancelled'):
            return 'Completed, Cancelled', 0
        wait, run = 0.8, 4.0 / self.speed
        if age < wait:
            return 'Queued, Remotely', 0
        if age < wait + run:
            return 'InProgress', int(t['size'] * (age - wait) / run)
        return 'Completed, Succeeded', t['size']

    def write_file(self, t):
        if t.get('written'):
            return
        t['written'] = True
        folder = self.downloads / t['destination']
        folder.mkdir(parents=True, exist_ok=True)
        base = t['filename'].replace('\\', '/').rsplit('/', 1)[-1]
        fmt = base.rsplit('.', 1)[-1].lower()
        head = HEADERS[fmt]
        (folder / base).write_bytes(head + b'\0' * (t['size'] - len(head)))


def make_app(fake: Fake):
    async def server(request):
        return web.json_response({'isLoggedIn': fake.logged_in, 'state': 'Connected, LoggedIn'})

    async def start_search(request):
        body = await request.json()
        fake.searches[body['id']] = {'text': body['searchText'], 'started': time.monotonic()}
        return web.json_response({'id': body['id']})

    async def get_search(request):
        s = fake.searches[request.match_info['id']]
        done = time.monotonic() - s['started'] > fake.search_seconds
        return web.json_response({'isComplete': done, 'state': 'Completed, TimedOut' if done else 'InProgress',
                                  'responseCount': len(fake.responses(s['text'])) if done else 0})

    async def search_responses(request):
        return web.json_response(fake.responses(fake.searches[request.match_info['id']]['text']))

    async def ok(request):
        return web.json_response({})

    async def enqueue(request):
        body = await request.json()
        transfers = []
        for f in body['files']:
            tid = str(uuid.uuid4())
            fake.transfers[tid] = {
                'created': time.monotonic(), 'size': f['size'], 'filename': f['filename'],
                'username': body['username'], 'destination': body['options']['destination'],
            }
            transfers.append({'id': tid})
        return web.json_response({'batch': {'transfers': transfers}, 'failures': []})

    async def get_download(request):
        t = fake.transfers.get(request.match_info['tid'])
        if not t:
            return web.Response(status=404, text='not found')
        state, got = fake.transfer_state(t)
        if state == 'Completed, Succeeded':
            fake.write_file(t)
        return web.json_response({'state': state, 'bytesTransferred': got, 'averageSpeed': 1_000_000})

    async def position(request):
        return web.json_response(2)

    async def cancel(request):
        t = fake.transfers.get(request.match_info['tid'])
        if t:
            t['cancelled'] = True
        return web.json_response({})

    app = web.Application()
    base = '/api/v0'
    app.add_routes([
        web.get(f'{base}/server', server),
        web.post(f'{base}/searches', start_search),
        web.get(f'{base}/searches/{{id}}', get_search),
        web.put(f'{base}/searches/{{id}}', ok),
        web.delete(f'{base}/searches/{{id}}', ok),
        web.get(f'{base}/searches/{{id}}/responses', search_responses),
        web.post(f'{base}/transfers/downloads/batches', enqueue),
        web.get(f'{base}/transfers/downloads/{{user}}/{{tid}}', get_download),
        web.get(f'{base}/transfers/downloads/{{user}}/{{tid}}/position', position),
        web.delete(f'{base}/transfers/downloads/{{user}}/{{tid}}', cancel),
        web.put(f'{base}/shares', ok),
    ])
    app[web.AppKey('fake', Fake)] = fake
    return app


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--downloads', default='downloads')
    ap.add_argument('--port', type=int, default=5030)
    ap.add_argument('--speed', type=float, default=1.0, help='download speed; 0.25 makes downloads four times as slow')
    args = ap.parse_args()
    web.run_app(make_app(Fake(Path(args.downloads).resolve(), args.speed)), port=args.port, print=None)
