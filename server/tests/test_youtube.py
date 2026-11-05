"""Reading a YouTube playlist link. yt-dlp is replaced by canned answers; the
titles are real ones from a public playlist."""

import pytest

from songdl import playlist, youtube
from songdl.errors import UserError

from tests.test_site import scan

PID = 'PLoaTDHRuxwgxk0WTXRJJJlXUYuHBSrbcF'


@pytest.mark.parametrize('title, channel, artist, song', [
    ('Michael Jackson - Billie Jean (Official Video)', 'Michael Jackson', 'Michael Jackson', 'Billie Jean'),
    ('Queen – Bohemian Rhapsody (Official Video Remastered)', 'Queen Official', 'Queen', 'Bohemian Rhapsody'),
    ('Green Day - American Idiot [Official Music Video] [4K Upgrade]', 'Green Day', 'Green Day', 'American Idiot'),
    ('Kate Bush - Wuthering Heights - Official Music Video - Version 1', 'KateBushMusic', 'Kate Bush', 'Wuthering Heights'),
    ('Kim Wilde - Kids In America (1981) • TopPop', 'TopPop', 'Kim Wilde', 'Kids In America'),
    ('Kanye West - Gold Digger ft. Jamie Foxx', 'Kanye West', 'Kanye West', 'Gold Digger'),
    ('Nick Cave & The Bad Seeds ft. Kylie Minogue - Where The Wild Roses Grow', 'Nick Cave',
     'Nick Cave & The Bad Seeds', 'Where The Wild Roses Grow'),
    ('BLACK SABBATH - "Paranoid" (Official Video)', 'Black Sabbath', 'BLACK SABBATH', 'Paranoid'),
    ('Elvis Presley "Don\'t Be Cruel" (January 6, 1957) on The Ed Sullivan Show', 'The Ed Sullivan Show',
     'Elvis Presley', "Don't Be Cruel"),
    ('Metallica: Enter Sandman (Official Music Video)', 'Metallica', 'Metallica', 'Enter Sandman'),
    ('Black Velvet by Alannah Myles', 'Alannah Myles', 'Alannah Myles', 'Black Velvet'),
    ('Losing My Religion - R.E.M.', 'R.E.M.', 'R.E.M.', 'Losing My Religion'),
    # No artist in the title: the channel is the artist
    ('Creep', 'Radiohead', 'Radiohead', 'Creep'),
    ('Summer Nights (From “Grease”)', 'John Travolta - Topic', 'John Travolta', 'Summer Nights (From “Grease”)'),
    ('Wild Horses', 'GinoVannelliVEVO', 'Gino Vannelli', 'Wild Horses'),
    ('Zombie', 'TheCranberriesTV', 'The Cranberries', 'Zombie'),
    # After the dash is which recording, not the song
    ('Dreams - 2004 Remaster', 'Fleetwood Mac', 'Fleetwood Mac', 'Dreams - 2004 Remaster'),
    ('Song 2 - Live at Glastonbury 1999', 'Blur', 'Blur', 'Song 2 - Live at Glastonbury 1999'),
    ('Jay-Z - 99 Problems', 'JayZVEVO', 'Jay-Z', '99 Problems'),
])
def test_titles_are_split_into_artist_and_song(title, channel, artist, song):
    assert youtube.split(title, channel) == (artist, song)


def test_playlist_links_are_recognised():
    for link in (f'https://www.youtube.com/playlist?list={PID}',
                 f'https://www.youtube.com/watch?v=HU7KlDJVINc&list={PID}&index=3',
                 f'https://music.youtube.com/playlist?list={PID}&si=x',
                 f'https://m.youtube.com/playlist?list={PID}',
                 f'https://youtu.be/HU7KlDJVINc?list={PID}',
                 f'youtube.com/playlist?list={PID}'):
        assert youtube.playlist_id(link) == PID, link
    for link in ('https://www.youtube.com/watch?v=HU7KlDJVINc', f'https://example.com/playlist?list={PID}',
                 'https://www.youtube.com/playlist?list=WL'):
        assert youtube.playlist_id(link) is None, link


def test_every_way_youtube_shares_a_playlist():
    for link in ('https://www.youtube.com/playlist?list=PLKxmRL6AnoXI',
                 'https://youtube.com/playlist?list=PLKxmRL6AnoXI&si=NOU5R7FTFQRmu_AK',
                 'https://youtu.be/playlist?list=PLKxmRL6AnoXI',
                 'https://www.youtube.com/watch?v=SMVqAJcX9I4&list=PLKxmRL6AnoXI&index=1',
                 'https://music.youtube.com/playlist?list=PLKxmRL6AnoXI&si=pXC_Htm0K52WiQIv'):
        assert youtube.playlist_id(link) == 'PLKxmRL6AnoXI', link
    for video in ('https://www.youtube.com/watch?v=SMVqAJcX9I4',
                  'https://youtu.be/SMVqAJcX9I4?si=qEkpPHs7BJS4Lpkg',
                  'https://music.youtube.com/watch?v=CQ3Ya0ny1wo'):
        assert youtube.playlist_id(video) is None, video


def entry(title, channel, duration=200, vid='abc'):
    return {'_type': 'url', 'id': vid, 'url': f'https://www.youtube.com/watch?v={vid}',
            'title': title, 'channel': channel, 'duration': duration}


INFO = {'_type': 'playlist', 'title': 'Road Trip', 'channel': 'someone', 'playlist_count': 4, 'entries': [
    entry('Daft Punk - Digital Love (Official Video)', 'Daft Punk', 301, 'a'),
    entry('Barbra Streisand', 'Duck Sauce - Topic', 194, 'b'),
    {'_type': 'url', 'id': 'c', 'url': 'https://www.youtube.com/watch?v=c', 'title': None, 'channel': None},
    entry('Nobody - No Such Song', 'Nobody', 180, 'd'),
]}


@pytest.fixture
def fake_ytdlp(monkeypatch):
    answer = {'info': INFO}
    monkeypatch.setattr(youtube, '_extract', lambda pid: answer['info'])
    return answer


async def test_the_whole_playlist_becomes_a_csv(fake_ytdlp):
    found = await youtube.YouTube().playlist(PID)
    assert (found.name, found.owner, len(found.rows), found.unavailable) == ('Road Trip', 'someone', 4, 1)
    # The unavailable video stays in the CSV, with only its address
    assert found.rows[2]['Video URL'].endswith('v=c') and found.rows[2]['Track Name'] == ''

    tracks, skipped = playlist.parse(found.csv().encode())
    assert skipped == 1
    assert [(t.artist, t.title, t.duration) for t in tracks] == [
        ('Daft Punk', 'Digital Love', None),     # a music video's length isn't the song's
        ('Duck Sauce', 'Barbra Streisand', 194),
        ('Nobody', 'No Such Song', None),
    ]


async def test_a_short_listing_is_an_error_not_a_short_csv(fake_ytdlp):
    fake_ytdlp['info'] = {**INFO, 'playlist_count': 40}
    with pytest.raises(UserError) as e:
        await youtube.YouTube().playlist(PID)
    assert e.value.status == 502


async def test_mixes_are_refused():
    with pytest.raises(UserError) as e:
        await youtube.YouTube().playlist('RDHU7KlDJVINc')
    assert 'Mix' in str(e.value)


async def test_playlist_link_endpoint_reads_youtube(site, fake_ytdlp):
    res = await site.post('/api/playlist-link', json={'url': f'https://music.youtube.com/playlist?list={PID}'})
    assert res.status == 200, await res.text()
    body = await res.json()
    assert (body['source'], body['name'], body['total'], body['unavailable']) == ('youtube', 'Road Trip', 3, 1)
    job = await scan(site, body['csv'], 'flac')
    assert (job['total'], job['skipped']) == (3, 1)
    assert [t['state'] for t in job['tracks']] == ['found', 'found', 'missing']

    res = await site.post('/api/playlist-link', json={'url': 'https://www.youtube.com/watch?v=HU7KlDJVINc'})
    assert res.status == 400
