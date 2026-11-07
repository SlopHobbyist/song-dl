"""YouTube as the fallback when Soulseek doesn't have a song. yt-dlp is
replaced by FakeYouTube; the scoring is tested on titles like real ones."""

import time
import types
from pathlib import Path

import pytest

from songdl import youtube_audio
from songdl.youtube_audio import score

from tests.test_site import fetch, finished, queue_view, scan, wait_for

OPUS = b'OggS' + b'\0' * 60_000


def video(vid, title, channel, duration=240, views=1000):
    return {'id': vid, 'title': title, 'channel': channel, 'duration': duration, 'view_count': views}


class FakeYouTube:
    """Stands in for yt-dlp: a few videos for any search, and an Opus file for any download."""

    def __init__(self):
        self.searches = []    # queries
        self.probed = []      # video URLs
        self.downloaded = []  # video URLs
        self.fail = None      # an exception every download raises
        self.results = [
            video('aaaaaaaaaaa', 'Nobody - Never Heard (Live at Wembley)', 'Nobody', views=10**6),
            video('bbbbbbbbbbb', 'Nobody - Never Heard (Official Video)', 'NobodyVEVO', views=10**6),
            video('ccccccccccc', 'Never Heard', 'Nobody - Topic', views=10**4),
            video('ddddddddddd', 'Never Heard 1 hour loop', 'Loops', duration=3600),
        ]

    def search(self, cfg, query):
        self.searches.append(query)
        return list(self.results)

    def probe(self, cfg, url):
        self.probed.append(url)
        vid = url.rsplit('=', 1)[-1]
        found = next((v for v in self.results if v['id'] == vid), video(vid, 'Some Artist - Pasted Song', 'Some Artist'))
        return {**found, 'acodec': 'opus', 'ext': 'webm', 'abr': 129.6, 'asr': 48000, 'filesize': len(OPUS)}

    def download(self, cfg, cand, folder, fetch):
        self.downloaded.append(cand.url)
        if self.fail:
            raise self.fail
        folder.mkdir(parents=True, exist_ok=True)
        fetch.phase, fetch.total = 'transferring', len(OPUS)
        for done in (20_000, 40_000, len(OPUS)):
            fetch.bytes = done
            time.sleep(0.05)
        path = folder / (cand.url.rsplit('=', 1)[-1] + '.opus')
        path.write_bytes(OPUS)
        return path


@pytest.fixture
def yt(monkeypatch):
    f = FakeYouTube()
    monkeypatch.setattr(youtube_audio, '_search', f.search)
    monkeypatch.setattr(youtube_audio, '_probe', f.probe)
    monkeypatch.setattr(youtube_audio, '_download', f.download)
    return f


async def search(client, query, formats, sources, **extra):
    res = await client.post('/api/search', json={'query': query, 'formats': formats, 'sources': sources, **extra})
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']
    return await wait_for(lambda: _json(client.get(f'/api/search/{sid}')), lambda v: v['state'] != 'running')


async def _json(request):
    return await (await request).json()


# Scoring

def test_the_album_track_beats_the_music_video_and_a_fans_upload():
    topic = score(video('a' * 11, 'Digital Love', 'Daft Punk - Topic', 301), 'daft punk digital love')[0]
    audio = score(video('b' * 11, 'Daft Punk - Digital Love (Official Audio)', 'Daft Punk', 301), 'daft punk digital love')[0]
    clip = score(video('c' * 11, 'Daft Punk - Digital Love (Official Video)', 'Daft Punk', 265), 'daft punk digital love')[0]
    fan = score(video('d' * 11, 'Daft Punk - Digital Love', 'DHardcore2008', 299), 'daft punk digital love')[0]
    assert topic > fan and audio > fan and audio > clip


@pytest.mark.parametrize('title, channel, duration', [
    ('Daft Punk - Digital Love (Live)', 'Daft Punk', 300),
    ('Digital Love - Daft Punk (Piano Cover)', 'Pianist', 300),
    ('Daft Punk-Digital Love (1 hour loop)', 'Love Soldier', 3658),
    ('Daft Punk : Gap Commercial Spot (HD)', 'Daft Punk – Archives', 33),
    ('Daft Punk - One More Time', 'Daft Punk', 320),
])
def test_videos_that_arent_the_song_are_dropped(title, channel, duration):
    assert score(video('x' * 11, title, channel, duration), 'Daft Punk Digital Love', 'Daft Punk', 'Digital Love')[0] is None


def test_a_remix_is_kept_when_it_was_asked_for():
    entry = video('x' * 11, 'Duck Sauce - Barbra Streisand (Remix)', 'Duck Sauce', 200)
    assert score(entry, 'duck sauce barbra streisand remix')[0] is not None


def test_the_playlists_length_picks_the_recording():
    same = score(video('a' * 11, 'Daft Punk - Digital Love', 'Daft Punk', 300), 'Daft Punk Digital Love', 'Daft Punk', 'Digital Love', 301)
    edit = score(video('b' * 11, 'Daft Punk - Digital Love', 'Daft Punk', 241), 'Daft Punk Digital Love', 'Daft Punk', 'Digital Love', 301)
    assert same[0] > edit[0] and not same[1] and edit[1]


# Searching

async def test_a_song_soulseek_doesnt_have_comes_from_youtube(site, yt):
    job = await search(site, 'nobody never heard', ['flac'], ['soulseek', 'youtube'])
    assert job['state'] == 'done' and job['missing'] == ['FLAC'] and job['sources'] == ['Soulseek', 'YouTube']
    [r] = job['results']
    assert (r['source'], r['format'], r['quality'], r['from']) == ('youtube', 'Opus', '130 kbps', 'Nobody - Topic')
    assert r['link'] == 'https://www.youtube.com/watch?v=ccccccccccc'   # the album track, not the live one
    assert (job['title'], job['artist'], r['filename']) == ('Never Heard', 'Nobody', 'Nobody - Never Heard.opus')
    assert yt.searches == ['nobody never heard'] and len(yt.probed) == 1

    jid = await fetch(site, r['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['state'] == 'done' and done['name'] == 'Nobody - Never Heard.opus', done
    file = await site.get(done['file'])
    assert file.status == 200 and (await file.read()) == OPUS


async def test_youtube_isnt_asked_when_soulseek_has_the_song(site, yt):
    job = await search(site, 'daft punk digital love', ['flac'], ['soulseek', 'youtube'])
    assert [r['source'] for r in job['results']] == ['soulseek'] and yt.searches == []


async def test_youtube_alone_needs_no_formats_or_soulseek(site, yt, fake):
    fake.logged_in = False
    job = await search(site, 'nobody never heard', [], ['youtube'])
    assert [r['source'] for r in job['results']] == ['youtube'] and job['missing'] == []
    jid = await fetch(site, job['results'][0]['id'])
    assert (await finished(site, [jid]))[jid]['state'] == 'done'


async def test_a_pasted_video_is_the_one_downloaded(site, yt):
    job = await search(site, 'some artist pasted song', ['flac'], ['soulseek', 'youtube'], video='zzzzzzzzzzz')
    assert yt.searches == [] and yt.probed == ['https://www.youtube.com/watch?v=zzzzzzzzzzz']
    assert job['results'][0]['link'].endswith('zzzzzzzzzzz') and job['title'] == 'Pasted Song'


async def test_nothing_anywhere(site, yt):
    yt.results = []
    job = await search(site, 'nobody never heard', ['flac'], ['soulseek', 'youtube'])
    assert job['results'] == [] and job['missing'] == ['FLAC']


async def test_sources_are_checked(site, yt):
    for sources, message in ((['napster'], 'Pick'), ([], 'Pick')):
        res = await site.post('/api/search', json={'query': 'nobody never heard', 'formats': ['flac'], 'sources': sources})
        assert res.status == 400 and message in (await res.json())['error'], sources
    # Left out: Soulseek only, as before
    job = await (await site.post('/api/search', json={'query': 'nobody never heard', 'formats': ['flac']})).json()
    job = await wait_for(lambda: _json(site.get(f'/api/search/{job["id"]}')), lambda v: v['state'] != 'running')
    assert job['results'] == [] and yt.searches == []


async def test_a_failed_youtube_download_says_why(site, yt):
    yt.fail = Exception('ERROR: [youtube] ccccccccccc: Sign in to confirm you\'re not a bot')
    job = await search(site, 'nobody never heard', ['flac'], ['youtube'])
    jid = await fetch(site, job['results'][0]['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['state'] == 'failed' and 'prove it isn\'t a bot' in done['error'], done
    # The album track, then the music video; the live take and the loop aren't the song
    assert yt.downloaded == ['https://www.youtube.com/watch?v=ccccccccccc', 'https://www.youtube.com/watch?v=bbbbbbbbbbb']


# Playlists

async def test_a_scan_falls_back_to_youtube(site, yt):
    res = await site.post('/api/scan', params={'formats': 'flac', 'sources': 'soulseek,youtube', 'name': 't.csv'},
                          data=b'title,artist\nDigital Love,Daft Punk\nNever Heard,Nobody\n')
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']
    job = await wait_for(lambda: _json(site.get(f'/api/scan/{sid}')), lambda v: v['state'] != 'running')
    assert [t['state'] for t in job['tracks']] == ['found', 'found']
    assert [[r['source'] for r in t['results']] for t in job['tracks']] == [['soulseek'], ['youtube']]
    assert yt.searches == ['Nobody Never Heard']


async def test_a_scan_without_youtube_is_unchanged(site, yt):
    job = await scan(site, 'title,artist\nNever Heard,Nobody\n', 'flac')
    assert job['tracks'][0]['state'] == 'missing' and yt.searches == []


async def test_an_admin_queues_a_youtube_song(admin, yt):
    job = await search(admin, 'nobody never heard', ['flac'], ['soulseek', 'youtube'])
    res = await admin.post('/api/queue', json={'results': [job['results'][0]['id']], 'folder': 'From YT'})
    assert (await res.json())['queued'] == 1
    view = await wait_for(lambda: queue_view(admin), lambda v: v['items'] and v['items'][0]['status'] == 'done')
    item = view['items'][0]
    assert (item['format'], item['source'], item['note']) == ('Opus', 'youtube', 'Saved as Nobody - Never Heard.opus')
    assert (admin.cfg.downloads_dir / 'From YT' / 'Nobody - Never Heard.opus').read_bytes() == OPUS


async def test_an_import_queues_songs_soulseek_doesnt_have_from_youtube(admin, yt):
    res = await admin.post('/api/imports', json={
        'csv': 'title,artist\nDigital Love,Daft Punk\nNever Heard,Nobody\n', 'name': 'mix', 'formats': ['flac'],
        'sources': ['soulseek', 'youtube'], 'folder': 'Mix'})
    assert res.status == 200, await res.text()
    iid = (await res.json())['id']

    async def view():
        return next(i for i in (await _json(admin.get('/api/imports')))['imports'] if i['id'] == iid)

    imp = await wait_for(view, lambda v: v['state'] == 'done')
    assert (imp['found'], imp['missing'], imp['queued'], imp['sources']) == (2, 0, 2, ['Soulseek', 'YouTube'])
    items = (await wait_for(lambda: queue_view(admin),
                            lambda v: len(v['items']) == 2 and all(i['status'] == 'done' for i in v['items'])))['items']
    assert sorted(i['format'] for i in items) == ['FLAC', 'Opus']


# A network hiccup during a download is tried again

_real_download = youtube_audio._download   # conftest swaps it for an offline stub in every test


class FlakyYoutubeDL:
    """Fails each extract_info with the next error in `errors`, then saves a file."""
    errors = []
    calls = 0

    def __init__(self, options):
        self.folder = Path(options['outtmpl']).parent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        FlakyYoutubeDL.calls += 1
        if FlakyYoutubeDL.errors:
            raise FlakyYoutubeDL.errors.pop(0)
        return {'id': 'x'}

    def process_ie_result(self, info, download=True):
        path = self.folder / 'x.m4a'
        path.write_bytes(b'audio')
        return {'requested_downloads': [{'filepath': str(path)}]}


@pytest.fixture
def flaky(monkeypatch):
    import yt_dlp
    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FlakyYoutubeDL)
    monkeypatch.setattr(youtube_audio, 'RETRY_DELAYS', (0, 0))
    FlakyYoutubeDL.calls = 0
    return FlakyYoutubeDL


def _cand():
    return types.SimpleNamespace(url='https://soundcloud.com/u/t', meta={})


DNS = Exception("Unable to download JSON metadata: Failed to resolve 'api-v2.soundcloud.com' "
                "([Errno -2] Name or service not known)")


def test_a_dns_hiccup_is_tried_again(flaky, tmp_path):
    flaky.errors = [DNS]
    fetch = types.SimpleNamespace(cancelled=False)
    path = _real_download(None, _cand(), tmp_path / 'f', fetch)
    assert path.read_bytes() == b'audio' and flaky.calls == 2


def test_the_network_staying_down_gives_up(flaky, tmp_path):
    flaky.errors = [DNS, DNS, DNS, DNS]
    with pytest.raises(Exception, match='Failed to resolve'):
        _real_download(None, _cand(), tmp_path / 'f', types.SimpleNamespace(cancelled=False))
    assert flaky.calls == 3 and not (tmp_path / 'f').exists()


def test_other_errors_arent_tried_again(flaky, tmp_path):
    flaky.errors = [Exception('This video is DRM protected')]
    with pytest.raises(Exception, match='DRM'):
        _real_download(None, _cand(), tmp_path / 'f', types.SimpleNamespace(cancelled=False))
    assert flaky.calls == 1


def test_network_trouble_is_named():
    from songdl import soundcloud_audio
    assert soundcloud_audio.download_error(DNS) == 'The server couldn\'t reach SoundCloud'
    assert youtube_audio.download_error(Exception('Read timed out')) == 'The server couldn\'t reach YouTube'
