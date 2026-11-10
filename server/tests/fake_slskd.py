"""A stand-in for slskd, for tests and for trying the site without Soulseek.

    python -m tests.fake_slskd --downloads ../downloads --port 5030

It serves the parts of slskd's API that Song-DL uses and a small made-up music
library. Searches take about a second; downloads take a few seconds and write
a file with the right header and size into the downloads folder. With
--captcha alice=download, alice refuses downloads and messages us a captcha
until we message back "download".
"""

import argparse
import asyncio
import time
import uuid
from datetime import datetime, timezone
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
        self.busy = set()        # kinds of request (searches, transfers) slskd is in the middle of
        self.refused = 0
        self.transfers = {}
        self.logged_in = True
        self.captchas = {}       # username -> keyword they want messaged back before they upload
        self.whitelisted = set()
        self.messages = {}       # username -> private messages, both ways, as slskd lists them
        self.message_id = 0

    def say(self, user, text, replayed=False, timestamp=None):
        """A private message from `user` to us."""
        self._add(user, text, 'In', replayed, timestamp)

    def sent(self, user):
        return [m['message'] for m in self.messages.get(user, []) if m['direction'] == 'Out']

    def unread(self):
        return [m for ms in self.messages.values() for m in ms if not m['isAcknowledged']]

    def _add(self, user, text, direction, replayed=False, timestamp=None):
        self.message_id += 1
        # slskd's timestamps are UTC, without an offset
        when = timestamp or datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self.messages.setdefault(user, []).append({
            'timestamp': when, 'id': self.message_id, 'username': user, 'direction': direction,
            'message': text, 'isAcknowledged': direction == 'Out', 'wasReplayed': replayed,
        })

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
        if t.get('captcha'):
            return ('Completed, Rejected', 0) if age > 0.3 else ('Requested', 0)
        wait, run = 0.8, 4.0 / self.speed
        if age < wait:
            return 'Queued, Remotely', 0
        if age < wait + run:
            return 'InProgress', int(t['size'] * (age - wait) / run)
        return 'Completed, Succeeded', t['size']

    def write_file(self, t):
        if not t.get('written'):
            t['written'] = asyncio.create_task(self._move_file(t))

    async def _move_file(self, t):
        """Like slskd, which says a download succeeded and then moves it out of its incomplete
        folder: a copy, when that's on another mount, so the file shows up late and grows."""
        folder = self.downloads / t['destination']
        base = t['filename'].replace('\\', '/').rsplit('/', 1)[-1]
        fmt = base.rsplit('.', 1)[-1].lower()
        data = HEADERS[fmt] + b'\0' * (t['size'] - len(HEADERS[fmt]))
        await asyncio.sleep(0.3)
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / base).open('wb') as f:
            f.write(data[:len(data) // 2])
            f.flush()
            await asyncio.sleep(0.3)
            f.write(data[len(data) // 2:])


def make_app(fake: Fake):
    async def server(request):
        return web.json_response({'isLoggedIn': fake.logged_in, 'state': 'Connected, LoggedIn'})

    def one_at_a_time(handler):
        """Like slskd: a search is started, and a download enqueued, one at a time. Any other
        asked for meanwhile is refused."""
        async def wrapped(request):
            kind = request.path.split('/')[3]
            if kind in fake.busy:
                fake.refused += 1
                return web.json_response('Only one concurrent operation is permitted. Wait until the previous '
                                         'request completes', status=429)
            fake.busy.add(kind)
            try:
                await asyncio.sleep(0.05)
                return await handler(request)
            finally:
                fake.busy.discard(kind)
        return wrapped

    @one_at_a_time
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
        # Like slskd, which says a search is complete a moment before its responses are listed
        s = fake.searches[request.match_info['id']]
        if time.monotonic() - s['started'] < fake.search_seconds + 0.3:
            return web.json_response([])
        return web.json_response(fake.responses(s['text']))

    async def ok(request):
        return web.json_response({})

    @one_at_a_time
    async def enqueue(request):
        body = await request.json()
        transfers = []
        for f in body['files']:
            tid = str(uuid.uuid4())
            fake.transfers[tid] = {
                'created': time.monotonic(), 'size': f['size'], 'filename': f['filename'],
                'username': body['username'], 'destination': body['options']['destination'],
            }
            keyword = fake.captchas.get(body['username'])
            if keyword and body['username'] not in fake.whitelisted:
                fake.transfers[tid]['captcha'] = True
                fake.say(body['username'], f'ProveIt: To prove you are a human downloading these files, please '
                                           f'type "{keyword}" in this chat to be added to my whitelist.')
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

    async def conversations(request):
        unread = request.query.get('unAcknowledgedOnly') == 'true'
        return web.json_response([
            {'username': user, 'isActive': True, 'hasUnAcknowledgedMessages': any(not m['isAcknowledged'] for m in ms)}
            for user, ms in fake.messages.items() if not unread or any(not m['isAcknowledged'] for m in ms)])

    async def messages(request):
        unread = request.query.get('unAcknowledgedOnly') == 'true'
        ms = fake.messages.get(request.match_info['user'])
        if ms is None:
            return web.Response(status=404, text='not found')
        return web.json_response([m for m in ms if not unread or not m['isAcknowledged']])

    async def acknowledge(request):
        for m in fake.messages.get(request.match_info['user'], []):
            if m['id'] == int(request.match_info['id']):
                m['isAcknowledged'] = True
                return web.Response(status=200)
        return web.Response(status=404, text='not found')

    async def send(request):
        user, text = request.match_info['user'], await request.json()
        fake._add(user, text, 'Out')
        if fake.captchas.get(user) == text:
            fake.whitelisted.add(user)
        return web.Response(status=201)

    async def stop_search(request):
        # Like slskd: a PUT that doesn't say it's JSON isn't read
        if request.content_type != 'application/json':
            return web.Response(status=415, text='Unsupported Media Type')
        return web.json_response({})

    app = web.Application()
    base = '/api/v0'
    app.add_routes([
        web.get(f'{base}/server', server),
        web.post(f'{base}/searches', start_search),
        web.get(f'{base}/searches/{{id}}', get_search),
        web.put(f'{base}/searches/{{id}}', stop_search),
        web.delete(f'{base}/searches/{{id}}', ok),
        web.get(f'{base}/searches/{{id}}/responses', search_responses),
        web.post(f'{base}/transfers/downloads/batches', enqueue),
        web.get(f'{base}/transfers/downloads/{{user}}/{{tid}}', get_download),
        web.get(f'{base}/transfers/downloads/{{user}}/{{tid}}/position', position),
        web.delete(f'{base}/transfers/downloads/{{user}}/{{tid}}', cancel),
        web.put(f'{base}/shares', ok),
        web.get(f'{base}/conversations', conversations),
        web.get(f'{base}/conversations/{{user}}/messages', messages),
        web.put(f'{base}/conversations/{{user}}/{{id}}', acknowledge),
        web.post(f'{base}/conversations/{{user}}', send),
    ])
    app[web.AppKey('fake', Fake)] = fake
    return app


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--downloads', default='downloads')
    ap.add_argument('--port', type=int, default=5030)
    ap.add_argument('--speed', type=float, default=1.0, help='download speed; 0.25 makes downloads four times as slow')
    ap.add_argument('--captcha', action='append', default=[], metavar='USER=KEYWORD',
                    help='USER (alice or bob) refuses downloads until we message back KEYWORD')
    args = ap.parse_args()
    fake = Fake(Path(args.downloads).resolve(), args.speed)
    fake.captchas = dict(c.split('=', 1) for c in args.captcha)
    web.run_app(make_app(fake), port=args.port, print=None)
