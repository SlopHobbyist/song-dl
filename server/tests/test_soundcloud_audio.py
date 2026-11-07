"""SoundCloud as the fallback after Soulseek and before YouTube. The search is
replaced by tracks shaped like SoundCloud's API's, and the download by a fake."""

import time

import pytest

from songdl import soundcloud_audio
from songdl.soundcloud_audio import _stream, _track, score

from tests.test_site import fetch, finished, queue_view, wait_for
from tests.test_youtube_audio import FakeYouTube, search

M4A = b'\0\0\0\x20ftypM4A ' + b'\0' * 60_000

OPEN = [{'preset': 'aac_160k', 'format': {'protocol': 'hls'}, 'quality': 'sq', 'url': 'https://api/x/hls'},
        {'preset': 'aac_96k', 'format': {'protocol': 'hls'}, 'quality': 'lq', 'url': 'https://api/x/hls'}]
DRM = [{'preset': 'aac_160k', 'format': {'protocol': 'ctr-encrypted-hls'}, 'quality': 'sq', 'url': 'https://api/x/hls'},
       {'preset': 'aac_160k', 'format': {'protocol': 'cbc-encrypted-hls'}, 'quality': 'sq', 'url': 'https://api/x/hls'}]
PREVIEW = [{'preset': 'mp3_0_0', 'format': {'protocol': 'hls'}, 'snipped': True, 'url': 'https://api/x/preview/hls'}]


def item(tid, title, user, seconds=240, plays=1000, streams=OPEN, artist=None, full=None):
    slug = ''.join(c if c.isalnum() else '-' for c in title.lower())[:40].strip('-') or 'track'
    return {'kind': 'track', 'id': tid, 'title': title, 'user': {'username': user}, 'duration': seconds * 1000,
            'full_duration': (full or seconds) * 1000, 'playback_count': plays, 'policy': 'ALLOW',
            'permalink_url': f'https://soundcloud.com/{"".join(c for c in user.lower() if c.isalnum()) or "u"}/{slug}-{tid}',
            'media': {'transcodings': streams}, 'publisher_metadata': {'artist': artist} if artist else None}


class FakeSoundCloud:
    def __init__(self):
        self.searches = []
        self.downloaded = []
        self.fail = None          # an exception every download raises
        self.search_fails = False
        self.items = [
            item(1, 'Never Heard', 'Nobody', streams=DRM, plays=10**6, artist='Nobody'),   # the label's, under DRM
            item(2, 'Nobody - Never Heard (Somebody Flip)', 'Somebody'),
            item(3, 'Never Heard x Other Song', 'Mashups'),
            item(4, 'Nobody - Never Heard', 'Nobody Fan', plays=500),
            item(5, 'Nobody - Never Heard', 'Short Upload', seconds=180, plays=10**5),
        ]

    def search(self, cfg, query):
        self.searches.append(query)
        if self.search_fails:
            raise ConnectionError('SoundCloud is down')
        return [_track(i) for i in self.items]

    def download(self, cfg, cand, folder, fetch):
        self.downloaded.append(cand.url)
        if self.fail:
            raise self.fail
        folder.mkdir(parents=True, exist_ok=True)
        fetch.phase, fetch.total = 'transferring', len(M4A)
        for done in (20_000, len(M4A)):
            fetch.bytes = done
            time.sleep(0.05)
        path = folder / 'track.m4a'
        path.write_bytes(M4A)
        return path


@pytest.fixture
def sc(monkeypatch):
    f = FakeSoundCloud()
    monkeypatch.setattr(soundcloud_audio, '_search', f.search)
    monkeypatch.setattr(soundcloud_audio, '_download', f.download)
    return f


@pytest.fixture
def yt(monkeypatch):
    from songdl import youtube_audio
    f = FakeYouTube()
    monkeypatch.setattr(youtube_audio, '_search', f.search)
    monkeypatch.setattr(youtube_audio, '_probe', f.probe)
    monkeypatch.setattr(youtube_audio, '_download', f.download)
    return f


ALL = ['soulseek', 'soundcloud', 'youtube']


# What can be downloaded, and what's the song

def test_only_streams_anyone_may_play_count():
    assert _stream(item(1, 't', 'u')) == ('m4a', 160)
    assert _stream(item(1, 't', 'u', streams=DRM)) is None
    assert _stream(item(1, 't', 'u', streams=PREVIEW)) is None
    go_plus = [{'preset': 'aac_256k', 'format': {'protocol': 'hls'}, 'quality': 'hq', 'url': 'x'}] + OPEN[1:]
    assert _stream(item(1, 't', 'u', streams=go_plus)) == ('m4a', 96)
    assert _stream({**item(1, 't', 'u'), 'policy': 'BLOCK'}) is None


@pytest.mark.parametrize('title, user', [
    ('Fisher - Losing It (Stang Flip)', 'Stang'),
    ('Fisher x Kelis - Losing It x Milkshake', 'Control Room'),
    ('Yeah vs. Losing It', 'DUBLü'),
    ('Fisher - Losing It + September', 'tosutosu'),
    ('Fisher - Losing It', 'daycore audios'),
    ('Fisher - Losing It (Tiny Desk Version)', 'crescend'),
    ('Fisher - Losing It (Unreleased)', 'ids'),
])
def test_edits_and_mashups_arent_the_song(title, user):
    assert score(_track(item(9, title, user)), 'fisher losing it')[0] is None


def test_a_collaboration_is_the_song():
    t = _track(item(9, 'Fred again.. x Swedish House Mafia - Turn On The Lights again..', 'Fred again..'))
    assert score(t, 'fred again swedish house mafia turn on the lights again')[0] is not None
    # ...and so is a mashup that was asked for
    assert score(_track(item(9, 'Losing It x Milkshake', 'x')), 'losing it x milkshake')[0] is not None


def test_free_download_tags_are_left_out_of_the_name():
    assert _track(item(9, 'Jerri - The Jawn (Tone Edit) [FREE DOWNLOAD]', 'Tone'))['title'] == 'Jerri - The Jawn (Tone Edit)'


async def test_the_locked_upload_gives_the_length(sc):
    # Typed, so no length is known; the label's DRM upload says 240 s, so the 180 s upload is out
    found = await soundcloud_audio.SoundCloudAudio(None).find('nobody never heard')
    assert [c.username for c in found] == ['Nobody Fan']
    c = found[0]
    assert (c.fmt, c.bitrate, c.length, c.source, c.meta['title'], c.meta['artist']) == \
        ('m4a', 160, 240, 'soundcloud', 'Never Heard', 'Nobody')


# Searching

async def test_a_song_soulseek_doesnt_have_comes_from_soundcloud(site, sc, yt):
    job = await search(site, 'nobody never heard', ['flac'], ALL)
    assert job['state'] == 'done' and job['sources'] == ['Soulseek', 'SoundCloud', 'YouTube']
    [r] = job['results']
    assert (r['source'], r['format'], r['quality'], r['from']) == ('soundcloud', 'M4A', '160 kbps', 'Nobody Fan')
    assert r['link'].startswith('https://soundcloud.com/nobodyfan/')
    assert r['filename'] == 'Nobody - Never Heard.m4a'
    assert yt.searches == []   # SoundCloud had it, so YouTube wasn't asked

    jid = await fetch(site, r['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['state'] == 'done' and done['name'] == 'Nobody - Never Heard.m4a', done
    assert (await (await site.get(done['file'])).read()) == M4A


async def test_soundcloud_isnt_asked_when_soulseek_has_the_song(site, sc, yt):
    job = await search(site, 'daft punk digital love', ['flac'], ALL)
    assert [r['source'] for r in job['results']] == ['soulseek'] and sc.searches == []


async def test_youtube_is_next_when_soundcloud_has_nothing(site, sc, yt):
    sc.items = sc.items[:3]   # only the DRM upload, a flip and a mashup
    job = await search(site, 'nobody never heard', ['flac'], ALL)
    assert [r['source'] for r in job['results']] == ['youtube']
    assert sc.searches == ['nobody never heard'] and yt.searches == ['nobody never heard']


async def test_youtube_is_next_when_soundcloud_cant_be_searched(site, sc, yt):
    sc.search_fails = True
    job = await search(site, 'nobody never heard', ['flac'], ALL)
    assert job['state'] == 'done' and [r['source'] for r in job['results']] == ['youtube']


async def test_soundcloud_failing_alone_says_so(site, sc):
    sc.search_fails = True
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    assert job['state'] == 'failed' and 'SoundCloud' in job['error']


async def test_a_failed_soundcloud_download_says_why(site, sc):
    sc.fail = Exception('ERROR: [soundcloud] 1: This video is DRM protected')
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    jid = await fetch(site, job['results'][0]['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['state'] == 'failed' and 'Couldn\'t download it from SoundCloud' in done['error'], done
    assert 'only lets its app play' in done['error']


async def test_a_dns_hiccup_says_soundcloud_couldnt_be_reached(site, sc):
    sc.fail = Exception("ERROR: [soundcloud] u/removed-mix: Unable to download JSON metadata: Failed to resolve "
                        "'api-v2.soundcloud.com' ([Errno -2] Name or service not known)")
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    jid = await fetch(site, job['results'][0]['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['error'] == ('Couldn\'t download it from SoundCloud: the server couldn\'t reach SoundCloud.'
                             ' Please try again later.'), done


async def test_a_reason_naming_soundcloud_keeps_its_capital(site, sc):
    sc.fail = Exception('HTTP Error 429: Too Many Requests')
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    jid = await fetch(site, job['results'][0]['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['error'] == ('Couldn\'t download it from SoundCloud: SoundCloud is getting too many requests'
                             ' from Song-DL. Please try again in a few minutes.'), done


async def test_no_reason_isnt_said_twice(site, sc):
    sc.fail = Exception('something odd')
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    jid = await fetch(site, job['results'][0]['id'])
    done = (await finished(site, [jid]))[jid]
    assert done['error'] == 'Couldn\'t download it from SoundCloud. Please try again later.', done


# Playlists

async def test_a_scan_tries_soundcloud_then_youtube(site, sc, yt):
    sc.items = [item(4, 'Nobody - Never Heard', 'Nobody Fan', seconds=240)]
    csv = ('title,artist,duration\nDigital Love,Daft Punk,301\nNever Heard,Nobody,240\n'
           'Some Other,Nobody,200\n').encode()
    res = await site.post('/api/scan', params={'formats': 'flac', 'sources': ','.join(ALL), 'name': 't.csv'}, data=csv)
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']
    job = await wait_for(lambda: _json(site.get(f'/api/scan/{sid}')), lambda v: v['state'] != 'running')
    assert [[r['source'] for r in t['results']] for t in job['tracks']] == [['soulseek'], ['soundcloud'], []]
    # "Some Other" isn't on SoundCloud (its one track is another song) or YouTube (none of its videos match)
    assert sc.searches == ['Nobody Never Heard', 'Nobody Some Other'] and yt.searches == ['Nobody Some Other']


async def test_an_import_queues_a_soundcloud_track(admin, sc, yt):
    res = await admin.post('/api/imports', json={
        'csv': 'title,artist\nNever Heard,Nobody\n', 'name': 'mix', 'formats': ['flac'],
        'sources': ALL, 'folder': 'Mix'})
    assert res.status == 200, await res.text()
    items = (await wait_for(lambda: queue_view(admin),
                            lambda v: v['items'] and v['items'][0]['status'] == 'done'))['items']
    assert (items[0]['source'], items[0]['format']) == ('soundcloud', 'M4A')
    assert (admin.cfg.downloads_dir / 'Mix' / 'Nobody - Never Heard.m4a').read_bytes() == M4A
    assert yt.searches == []


async def _json(request):
    return await (await request).json()


async def test_a_poor_copy_is_left_to_youtube(site, sc, yt):
    sc.items = [item(4, 'Nobody - Never Heard', 'Nobody Fan', streams=OPEN[1:])]   # AAC 96k only
    job = await search(site, 'nobody never heard', [], ['soundcloud', 'youtube'])
    assert [r['source'] for r in job['results']] == ['youtube']
    # ...but taken when there's nowhere else to look
    job = await search(site, 'nobody never heard', [], ['soundcloud'])
    assert [(r['source'], r['quality']) for r in job['results']] == [('soundcloud', '96 kbps')]


# The order the visitor put the sources in

async def test_sources_are_tried_in_the_order_given(site, sc, yt):
    job = await search(site, 'nobody never heard', ['flac'], ['youtube', 'soundcloud', 'soulseek'])
    assert job['sources'] == ['YouTube', 'SoundCloud', 'Soulseek']
    assert [r['source'] for r in job['results']] == ['youtube'] and sc.searches == []


async def test_soulseek_can_come_after_soundcloud(site, sc, yt):
    job = await search(site, 'daft punk digital love', ['flac'], ['soundcloud', 'soulseek', 'youtube'])
    assert [r['source'] for r in job['results']] == ['soulseek']
    assert sc.searches == ['daft punk digital love'] and yt.searches == []


async def test_a_scan_tries_sources_in_the_order_given(site, sc, yt):
    csv = 'title,artist,duration\nDigital Love,Daft Punk,301\nNever Heard,Nobody,240\n'.encode()
    res = await site.post('/api/scan', params={'formats': 'flac', 'sources': 'soundcloud,soulseek', 'name': 't.csv'},
                          data=csv)
    assert res.status == 200, await res.text()
    sid = (await res.json())['id']
    job = await wait_for(lambda: _json(site.get(f'/api/scan/{sid}')), lambda v: v['state'] != 'running')
    assert job['sources'] == ['SoundCloud', 'Soulseek']
    assert [[r['source'] for r in t['results']] for t in job['tracks']] == [['soulseek'], ['soundcloud']]
    assert sorted(sc.searches) == ['Daft Punk Digital Love', 'Nobody Never Heard'] and yt.searches == []


async def test_an_import_tries_sources_in_the_order_given(admin, sc, yt):
    res = await admin.post('/api/imports', json={
        'csv': 'title,artist,duration\nNever Heard,Nobody,240\n', 'name': 'mix', 'formats': ['flac'],
        'sources': ['youtube', 'soundcloud', 'soulseek'], 'folder': 'Mix'})
    assert res.status == 200, await res.text()
    items = (await wait_for(lambda: queue_view(admin), lambda v: v['items']))['items']
    assert [i['source'] for i in items] == ['youtube'] and sc.searches == []
