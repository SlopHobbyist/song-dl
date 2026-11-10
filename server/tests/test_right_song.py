"""A playlist's song is only ever saved from a copy that is that song (matching.fit and
friends): by its own name, by one of its artists, in the version asked for, and of the
playlist's length, measured once it's here.

The first tests replay a real review: a DJ played all 220 downloads of a playlist and
marked each right (Y), wrong (W, or M when it wasn't the song at all) or a duplicate entry
(D). data/house_chunes_review.json has what the playlist asked for, the name of the file
that was downloaded, the length it advertised and the length it really played. 40 of the
220 were wrong. The checks have to refuse those, and keep the right ones.
"""

import json
from pathlib import Path

import pytest

from songdl import matching, soundcloud_audio, youtube_audio
from songdl.errors import UserError
from songdl.matching import Want, fit, length_fit, names_song
from songdl.playlist import Track, all_artists, _main_artist, parse

from tests.conftest import ADMIN, make_config, start_site
from tests.test_imports import import_view, start_import
from tests.test_site import queue_view, wait_for

REVIEW = json.loads((Path(__file__).parent / 'data' / 'house_chunes_review.json').read_text(encoding='utf-8'))

# Marked wrong for being a longer cut, with the right name. Longer versions are accepted (the
# reviewer kept eleven of them), so these pass too, but as 'longer': listed to be checked,
# and only taken when no copy of the playlist's length is to be had.
LONGER_BUT_WRONG = {'It Gets Better - Forever Mix', 'Full Moon'}
# Marked right, and refused: each is another version by its own name (a remix whose name was
# cut off, a "ReBoot", a DJ pool edit), and longer than the playlist's
OTHER_VERSIONS = {'Magnetic', 'WACUKA', 'What A Life (feat. Florence Arman)'}
# The same on SoundCloud and YouTube: someone else's upload of another length, and another mix
OTHER_UPLOADS = {"I'm Addicted", 'Make It Right', 'Do You Feel Me - Dosem Mix'}


def review():
    """[(row, the playlist's Track)] with the playlist's own versions marked, as an import does."""
    tracks = [Track(r['title'], _main_artist(r['artists']), r['album'], r['duration'],
                    artists=all_artists(r['artists'])) for r in REVIEW]
    matching.mark_versions(tracks)
    return [(r, t) for r, t in zip(REVIEW, tracks) if r['mark'] != 'D']


def soulseek_verdict(row, track):
    """What happens to the copy now: None when it is refused, before or after it's downloaded."""
    how = fit(row['name'], row['length'], track)
    fmt = row['name'].rsplit('.', 1)[-1].lower()
    if how is None or matching.too_small(fmt, row['size'], row['length'], row['bit_depth'], row['sample_rate']):
        return None
    if row['length'] and row['played'] < row['length'] - matching.LENGTH_TOLERANCE:
        return None   # less of it plays than it said
    return length_fit(row['played'], track.duration, matching.wants_exact(track))


def test_the_reviewed_soulseek_downloads():
    rows = [(r, t) for r, t in review() if r['source'] == 'soulseek']
    wrong = [(r, t) for r, t in rows if r['mark'] in 'WM']
    right = [(r, t) for r, t in rows if r['mark'] == 'Y']
    assert (len(wrong), len(right)) == (37, 144)

    passed = {r['title']: soulseek_verdict(r, t) for r, t in wrong if soulseek_verdict(r, t)}
    assert passed == dict.fromkeys(LONGER_BUT_WRONG, 'longer')

    refused = {r['title'] for r, t in right if not soulseek_verdict(r, t)}
    assert refused == OTHER_VERSIONS
    # The longer versions the reviewer kept are kept, and said to be longer
    assert sum(soulseek_verdict(r, t) == 'longer' for r, t in right) == 9


def test_the_name_alone_refuses_most_wrong_songs_and_no_right_one():
    rows = [(r, t) for r, t in review() if r['source'] == 'soulseek']
    assert all(names_song(r['name'], t) for r, t in rows if r['mark'] == 'Y')
    assert sum(not names_song(r['name'], t) for r, t in rows if r['mark'] in 'WM') == 25
    # Not one of the songs that weren't the song at all gets by
    assert not [r['title'] for r, t in rows if r['mark'] == 'M' and fit(r['name'], r['length'], t)]


def test_the_reviewed_soundcloud_and_youtube_downloads():
    kept, refused = set(), set()
    for r, t in review():
        if r['source'] == 'soulseek':
            continue
        title = r['name'].rsplit('.', 1)[0]
        args = (t.query, t.artist, t.title, t.duration)
        if r['source'] == 'soundcloud':
            upload = {'id': '1', 'title': title, 'user': r['uploader'], 'credited': '', 'duration': r['length'],
                      'full': r['length'], 'plays': 0, 'url': 'https://soundcloud.com/a/b', 'stream': ('m4a', 160)}
            points, _ = soundcloud_audio.score(upload, *args, artists=t.artists, exact=matching.wants_exact(t))
        else:
            entry = {'id': 'x' * 11, 'title': title, 'channel': r['uploader'], 'duration': r['length']}
            if r['uploader'] == 'Release - Topic':   # the artists are only in the description
                entry['description'] = f'Provided to YouTube by a label {title} · {" · ".join(t.artists)} ℗ 2026'
            points, _ = youtube_audio.score(entry, *args, artists=t.artists, exact=matching.wants_exact(t))
        (refused if points is None else kept).add((r['mark'], r['title']))
    assert not [title for mark, title in kept if mark != 'Y']
    assert {title for mark, title in refused if mark == 'Y'} == OTHER_UPLOADS
    assert sum(mark != 'Y' for mark, _ in refused) == 3


# The checks, one at a time

def track(title, artists, duration=None, album=''):
    return Track(title, _main_artist(artists), album, duration, artists=all_artists(artists))


def test_the_title_has_to_be_in_the_file_name_not_just_the_path():
    peak = track('Peak', '6am', 182)
    assert fit(r'shares\music\peak time\[2023] MATRODA - No Sleep (6AM)\01 - No Sleep (6AM).flac', 146, peak) is None
    assert fit(r'Z:\#\6AM\(2026) Peak\01-01. Peak.flac', 182, peak) == 'exact'
    # ...as the song's whole name: "Big Time" isn't Big Time Charlie's song, nor "My Heart" "Piece Of My Heart"
    assert not names_song('Secondcity, Monty Kiddo, Big Time Charlie - On The Run 2025.flac', track('Big Time', 'Monty Kiddo'))
    assert not names_song(r'Janis Zielinski\09 Piece Of My Heart.wav', track('my heart', 'Janis Zielinski'))
    assert names_song(r'music\Janis Zielinski - My Heart.wav', track('my heart', 'Janis Zielinski'))


def test_words_soulseek_ignores_are_checked_here():
    assert not names_song(r'Tech House\Jansons - Messan (Original Mix).wav', track('U Do', 'Jansons'))
    assert names_song(r'Tech House\Jansons - U Do (Original Mix).wav', track('U Do', 'Jansons'))


def test_one_word_of_the_artist_is_not_the_artist():
    sunset = track('Sunset', 'Flava D')
    assert not names_song(r'peter godwin\2009 Sunset Rise\d - 04 - Sunset.wav', sunset)   # only a "d"
    assert not names_song(r'd:\music\V-Tone - Sunset.wav', sunset)                        # a drive letter
    assert names_song(r'music\Flava D\Flava D - Sunset.wav', sunset)
    # Any one of the artists credited will do: a remix is often filed under its remixer
    remix = track('Closure - Soulfreq Remix', 'Soulfreq; Carlos Real')
    assert names_song(r'Carlos Real - Closure EP\05. Carlos Real - Closure  (Soulfreq Remix).flac', remix)
    assert names_song(r'Closure EP\05. Closure  (Soulfreq Remix).flac', remix)
    assert not names_song(r'Someone Else - Closure EP\05. Closure.flac', track('Closure', 'Soulfreq; Carlos Real'))


def test_spellings_that_are_the_same_name():
    assert names_song(r"dl\Julie McDermott - Dont Go.flac", track("Don't Go", 'Julie McDermott'))
    assert names_song(r'dl\Bausa - Magnetic.flac', track('Magnetic', 'The Bausa'))
    assert names_song(r'dl\Chris Stussy - Seeing and Believing.wav', track('Seeing & Believing', 'Chris Stussy'))
    assert names_song(r'dl\MadAgain - Misbehaving.wav', track('Misbehaving', 'Mad.Again'))
    assert names_song(r'dl\Filip Gronlund\01 Burning Desire.flac', track('Burning Desire', 'Filip Grönlund'))
    assert names_song(r'dl\NOCTRIS - Deeper.flac', track('Deeper', 'NØCTRIS'))
    assert names_song(r'dl\daft_punk-digital_love-uau.flac', track('Digital Love', 'Daft Punk'))
    assert names_song(r'Disclosure\Disclosure-The Face EP-003-Lividup.wav', track('Lividup', 'Disclosure'))
    assert names_song(r"rips\Paul-Johnson-It's-a-Boogie-Thang-01-It's-a-Love-Thang.wav", track("It's A Love Thang", 'Paul Johnson'))
    assert names_song(r"cdm\01 Nerio's Dubwork featuring Darryl Pandy - Sunshine & Happiness.wav",
                      track('Sunshine & Happiness', "Nerio's Dubwork feat. Darryl Pandy"))


def test_the_version_asked_for_has_to_be_in_the_file_name():
    remix = track("Ain't No Game - Clouds Remix", 'Duke Dumont; Clouds', 258)
    assert fit(r"Duke Dumont\Ain't No Game (Clouds Remix)\02 - Ain't No Game.flac", 161, remix) is None
    assert fit(r"Duke Dumont\Ain't No Game (Clouds Remix)\01 - Ain't No Game (Clouds Remix).flac", 258, remix) == 'exact'
    # A file of the remix's own length in the remix's folder is the remix, though, whatever it's called
    assert fit(r"Duke Dumont\Ain't No Game (Clouds Remix)\02 - Ain't No Game.flac", 258, remix) == 'exact'
    assert fit(r"Duke Dumont\Ain't No Game (Clouds Remix)\02 - Ain't No Game.flac", None, remix) is None
    # A year, or a mix by the artist, doesn't have to be spelled out
    assert names_song(r'Cerrone\Supernature\04 - give me love.wav', track('Give Me Love - 1977', 'Cerrone'))
    assert names_song(r'gold\38. Mousse T. - Horny (98 Extended Mix).wav', track("Horny - Mousse T.'s Extended Mix", 'Mousse T.'))


def test_other_versions_are_refused():
    song = track('Assumptions', 'Sam Gellaitry', 221)
    for name in (r'dl\Sam Gellaitry - Assumptions (Fabian Mazur Flip).wav',        # a flip
                 r'Mashups & Bootlegs\Sam Gellaitry - Assumptions.wav',            # the folder says so
                 r'Sam Gellaitry - Assumptions (9 Stems)\Sam Gellaitry - Assumptions.wav',
                 r"dl\Sam Gellaitry - Assumptions (MGR Mike's Disco Groove).wav",  # nothing the playlist said
                 r'dl\Sam Gellaitry - Assumptions (Subsonic.wav'):                 # cut off where it would say
        assert fit(name, 300, song) is None, name
    # ...but what is in brackets doesn't matter when it's the playlist's length, or says nothing
    assert fit(r'dl\(Grand Music) Sam Gellaitry - Assumptions.wav', 221, song) == 'exact'
    assert fit(r'dl\Sam Gellaitry - Assumptions (Original Mix).wav', 300, song) == 'longer'
    assert fit(r'dl\Sam Gellaitry - Assumptions (Original.wav', 300, song) == 'longer'


def test_the_length_has_to_agree_with_the_playlists():
    assert length_fit(None, None) == 'any' and length_fit(200, None) == 'any'
    assert length_fit(None, 200) == 'unknown'
    assert length_fit(195, 200) == length_fit(208, 200) == 'exact'
    assert length_fit(190, 200) is None                      # shorter is never the song
    assert length_fit(400, 200) == 'longer'                  # an extended version
    assert length_fit(400, 200, exact=True) is None
    assert length_fit(600, 200) is None                      # too long to be one


def test_a_playlist_with_two_versions_of_a_song_gets_each_at_its_own_length():
    tracks, _ = parse(b'title,artist,duration\nComes and Goes,KETTAMA,262\nComes and Goes - Extended,KETTAMA,434\n'
                      b'Picanya 2400 - Edit,KETTAMA,250\nYosemite,KETTAMA,266\n')
    matching.mark_versions(tracks)
    short, extended, edit, other = tracks
    long_file = r'soulseek uploads\Kettama - Comes and Goes (Original Mix).wav'
    assert fit(long_file, 433, short) is None       # the extended cut isn't the short entry
    assert fit(r'dl\Kettama - Comes and Goes (Extended).wav', 433, extended) == 'exact'
    # ...which is often shared as the "(Original Mix)": its length says it's the extended one
    assert fit(long_file, 433, extended) == 'exact' and fit(long_file, None, extended) is None
    # A song asked for as its edit is only its edit
    assert fit(r'dl\KETTAMA - Picanya 2400.wav', 320, edit) is None
    assert fit(r'dl\KETTAMA - Yosemite.wav', 400, other) == 'longer'


def test_a_file_too_small_for_its_length_is_a_clip(monkeypatch):
    monkeypatch.setattr(matching, 'MIN_FLAC_RATIO', 0.25)
    monkeypatch.setattr(matching, 'MIN_WAV_RATIO', 0.45)
    # A 30-second preview with a 4:37 header: 6.4 MB, where 24-bit audio that long is about 70
    assert matching.too_small('flac', 6_433_390, 277, 24, 44100)
    assert not matching.too_small('flac', 29_000_000, 277, 16, 44100)
    assert matching.too_small('wav', 5_000_000, 277) and not matching.too_small('wav', 48_000_000, 277)
    assert not matching.too_small('mp3', 1_000_000, 277)
    responses = [{'username': 'u', 'hasFreeUploadSlot': True, 'files': [
        {'filename': r'a\Calvin Harris - Blessings.flac', 'size': 6_433_390, 'length': 277, 'bitDepth': 24, 'sampleRate': 44100},
        {'filename': r'b\Calvin Harris - Blessings.flac', 'size': 60_000_000, 'length': 277, 'bitDepth': 24, 'sampleRate': 44100}]}]
    assert [c.size for c in matching.rank('calvin harris blessings', 'flac', responses).candidates] == [60_000_000]


def test_every_artist_of_a_row_is_kept():
    assert all_artists('Fred again..; Swedish House Mafia; Future') == ('Fred again..', 'Swedish House Mafia', 'Future')
    assert all_artists('Earth, Wind & Fire') == ('Earth, Wind & Fire',)
    assert all_artists('A, B') == ('A', 'B') and all_artists('') == ()
    tracks, _ = parse('title,artists\nMirage,MPH; Skrillex\n'.encode())
    assert (tracks[0].artist, tracks[0].artists) == ('MPH', ('MPH', 'Skrillex'))


# Searching Soulseek for a playlist's song

def shared(fake, filename, length, user='alice', size=400_000):
    fake.files.append(dict(user=user, free=True, queue=0, speed=1_000_000, filename=filename, size=size,
                           length=length, bitDepth=16, sampleRate=44100, bitRate=900, fmt='flac'))


async def test_a_search_only_returns_copies_that_are_the_song(site, fake):
    shared(fake, r'shares\music\peak time\[2023] MATRODA - No Sleep (6AM)\01 - No Sleep (6AM).flac', 146)
    peak = track('Peak', '6am', 182)
    with pytest.raises(UserError, match='No match found'):
        await site.engine.find_track(peak, 'flac')
    shared(fake, r'Z:\6AM\(2026) Peak\01-01. Peak.flac', 182, user='bob')
    cands = await site.engine.find_track(peak, 'flac')
    assert [(c.username, c.fit) for c in cands] == [('bob', 'exact')]


async def test_the_search_for_the_title_alone_still_needs_the_artist(site, fake):
    # Nothing has "flava d sunset" in its path, so the title is searched for alone
    shared(fake, r'peter godwin\2009 Sunset Rise\d - 04 - and I Will Follow.flac', 190)
    shared(fake, r'd\Somebody Else - Sunset.flac', 196)
    with pytest.raises(UserError, match='No match found'):
        await site.engine.find_track(track('Sunset', 'Flava D', 196), 'flac')


async def test_a_longer_version_is_only_offered_when_there_is_nothing_else(site, fake):
    song = track('Full Moon', 'Armand Van Helden; Common', 260)
    shared(fake, r'X-Mix\56 2000\05 Armand Van Helden feat Common - Full Moon.flac', 307)
    assert [(c.length, c.fit) for c in await site.engine.find_track(song, 'flac')] == [(307, 'longer')]
    shared(fake, r'Common\Armand Van Helden - Full Moon.flac', 259, user='bob')
    shared(fake, r'rips\Armand Van Helden - Full Moon.flac', None, user='bob')
    # The playlist's length first, then the copy that doesn't say; a busy uploader can't mean the long one
    assert [(c.length, c.fit) for c in await site.engine.find_track(song, 'flac')] == [(259, 'exact'), (None, 'unknown')]


# Once it's here, it is measured

async def test_a_download_that_plays_another_length_is_thrown_away(site, fake, monkeypatch):
    song = track('Digital Love', 'Daft Punk', 200)
    cands = await site.engine.find_track(song, 'flac')
    assert len(cands) == 2

    # A 30-second clip whose header said 3:20: neither copy is kept, and the files are gone
    monkeypatch.setattr('songdl.dj.played', lambda path: 30.2)
    with pytest.raises(UserError, match=r'Not saved: only 0:30 of it plays, and the file said 3:20') as e:
        await site.engine.download(cands, want=Want.of(song))
    assert e.value.status == 502 and len(fake.transfers) == 2
    assert not [p for p in site.cfg.downloads_dir.rglob('*.flac')]

    # It plays as long as the playlist says: kept, and its length written down
    monkeypatch.setattr('songdl.dj.played', lambda path: 201.4)
    fetch = await site.engine.download(cands, want=Want.of(song))
    assert fetch.path.exists() and (fetch.cand.measured, fetch.cand.fit) == (201, 'exact')


async def test_a_download_is_checked_against_the_playlist_not_just_itself(site, fake, monkeypatch):
    # The copy doesn't say how long it is, so it can only be told once it's here
    shared(fake, r'rips\Nobody - Never Heard.flac', None)
    song = track('Never Heard', 'Nobody', 240)
    cands = await site.engine.find_track(song, 'flac')
    assert [c.fit for c in cands] == ['unknown']
    monkeypatch.setattr('songdl.dj.played', lambda path: 150.0)
    with pytest.raises(UserError, match=r"it plays for 2:30, and the playlist's version is 4:00"):
        await site.engine.download(cands, want=Want.of(song))
    monkeypatch.setattr('songdl.dj.played', lambda path: 400.0)
    assert (await site.engine.download(cands, want=Want.of(song))).cand.fit == 'longer'
    with pytest.raises(UserError, match='Not saved'):
        await site.engine.download(cands, want=Want(240, exact=True))


# The Library says what each song was downloaded as, and takes "wrong song" for an answer

async def library_songs(client, playlist):
    return (await (await client.get('/api/library/songs', params={'playlist': playlist})).json())['songs']


async def test_a_longer_version_is_saved_and_listed_to_check(admin):
    # The playlist says 2:00; the only copies are 3:20
    iid = await start_import(admin, 'title,artist,duration\nDigital Love,Daft Punk,120\n', formats='flac')
    view = await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done' and v['saved'] == 1)
    assert (view['found'], view['check']) == (1, 1)
    item = (await queue_view(admin))['items'][0]
    assert item['note'].endswith("A longer version: 3:20, and the playlist's is 2:00")
    assert item['original'] == '01 - Digital Love.flac'
    song, = await library_songs(admin, 'Road Trip')
    assert song['original'] == '01 - Digital Love.flac' and song['source'] == 'soulseek'
    assert song['check'] == "A longer version: 3:20, and the playlist's is 2:00"


async def test_the_library_tells_a_song_from_the_file_it_was_downloaded_as(admin, fake):
    # Saved by an earlier version, under the playlist's name: only the library's record says what it is
    iid = await start_import(admin, 'title,artist\nDigital Love,Daft Punk\n', formats='flac')
    await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done' and v['saved'] == 1)
    song, = await library_songs(admin, 'Road Trip')
    assert (song['original'], song['check']) == ('01 - Digital Love.flac', None)
    lib = admin.engine.library
    rec = next(r for p, r in lib.songs.items() if 'Road Trip' in p)
    rec['name'] = r'shares\music\love songs\[2001] Somebody - Digital\07 - Analog Hate.flac'
    song, = await library_songs(admin, 'Road Trip')
    assert (song['original'], song['check']) == ('07 - Analog Hate.flac', 'Downloaded as a file with another name')


async def test_wrong_song_deletes_it_blocks_the_copy_and_looks_again(admin, fake):
    iid = await start_import(admin, 'title,artist\nDigital Love,Daft Punk\n', formats='wav,flac')
    await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done' and v['saved'] == 1)
    folder = admin.cfg.downloads_dir / 'Road Trip'
    assert [p.name for p in folder.iterdir()] == ['Daft Punk - Digital Love.wav']
    song, = await library_songs(admin, 'Road Trip')

    res = await admin.post('/api/library/wrong', json={'playlist': 'Road Trip', 'path': song['path']})
    assert res.status == 200 and (await res.json())['searching'] is True, await res.text()
    # That file name isn't taken for the song again, whoever shares it: the FLAC is
    lib = admin.engine.library
    assert list(lib.blocked.values()) == [['file:01 - digital love.wav']]
    view = await wait_for(lambda: import_view(admin, iid), lambda v: v['state'] == 'done' and v['saved'] == 1)
    assert view['found'] == 1
    assert [p.name for p in folder.iterdir()] == ['Daft Punk - Digital Love.flac']
    # ...and the wrong one is gone from the server, not just from the playlist
    assert not list(admin.cfg.downloads_dir.rglob('01 - Digital Love.wav'))
    assert not lib.find('daft punk digital love', ['wav'])

    assert (await admin.post('/api/library/wrong', json={'playlist': 'Road Trip', 'path': 'nope.flac'})).status == 404
    assert (await admin.post('/api/library/wrong', json={'playlist': 'Nope', 'path': song['path']})).status == 404


async def test_a_copy_on_the_server_is_only_reused_if_it_is_the_song(site):
    lib = site.engine.library
    folder = site.cfg.downloads_dir / '0b2615b4-9de7-4cf6-8c33-0b89e57cee04'
    folder.mkdir(parents=True)
    path = folder / '01 - No Sleep (6AM).flac'
    path.write_bytes(b'fLaC' + bytes(250_000))
    # Downloaded for "6am - Peak", and written down as that, but it never was
    lib._put({'path': str(path), 'title': 'Peak', 'artist': '6am', 'album': 'Peak', 'fmt': 'flac', 'size': 250_004,
              'length': 146, 'bit_depth': 16, 'sample_rate': 44100, 'bitrate': None, 'origin': 'soulseek',
              'name': r'shares\music\peak time\[2023] MATRODA - No Sleep (6AM)\01 - No Sleep (6AM).flac',
              'from': None, 'link': None, 'query': '', 'added': 1.0})
    assert lib.find('6am Peak', ['flac'])                                        # a typed search still sees it
    assert not lib.find('6am Peak', ['flac'], 182, track('Peak', '6am', 182))    # a playlist doesn't take it
    assert not lib.find('6am Peak', ['flac'], None, track('Peak', '6am'))        # ...with or without a length


async def test_songs_waiting_since_before_the_checks_are_checked_at_startup(tmp_path, fake):
    """The queue is kept through an update. What an older version put in it, it picked by the
    old rules: those copies are gone through again before anything is downloaded."""
    def copy(filename, length):
        return {'username': 'alice', 'filename': filename, 'size': 400_000, 'length': length, 'bit_depth': 16,
                'sample_rate': 44100, 'free_slot': True, 'queue': 0, 'speed': 1, 'fmt': 'flac'}

    def waiting(item_id, title, artist, cands):
        return {'id': item_id, 'title': title, 'artist': artist, 'album': '', 'format': 'flac', 'folder': 'Old',
                'by': 'admin', 'status': 'paused', 'dj': False, 'size': None, 'note': 'Paused', 'created': 1,
                'finished': 2, 'gone': False, 'cands': cands}

    wrong = copy(r'shares\music\peak time\[2023] MATRODA - No Sleep (6AM)\01 - No Sleep (6AM).flac', 146)
    right = copy(r'@@music\Daft Punk\Discovery\01 - Digital Love.flac', 200)
    live = copy(r'@@music\Daft Punk\Alive 2007\01 - Digital Love.flac', 150)
    cfg = make_config(tmp_path, fake)
    cfg.data_dir.mkdir(parents=True)
    cfg.queue_file.write_text(json.dumps({'version': 1, 'items': [
        waiting('one', 'Peak', '6am', [wrong]), waiting('two', 'Digital Love', 'Daft Punk', [live, right])]}))
    cfg.imports_file.write_text(json.dumps({'version': 1, 'imports': [{
        'id': 'old', 'name': 'old', 'formats': ['flac'], 'sources': ['soulseek'], 'folder': 'Old', 'by': 'admin',
        'dj': False, 'state': 'paused', 'note': '', 'created': 1, 'finished': None, 'skipped': 0,
        'tracks': [['Peak', '6am', 'Peak', 182, 'found', ['flac'], '', 'one', [], False],
                   ['Digital Love', 'Daft Punk', '', 200, 'found', ['flac'], '', 'two', [], False]]}]}))
    client = await start_site(tmp_path, fake)
    try:
        assert (await client.post('/api/login', json={'username': 'admin', 'password': ADMIN['admin']})).status == 200
        items = client.queue.items
        # The song that was never the song is out of the queue, to be looked for again...
        assert list(items) == ['two']
        view = await import_view(client, 'old')
        assert (view['found'], view['left']) == (1, 1)
        # ...and the other keeps only its copy of the playlist's length, which it will be measured against
        assert [c.filename for c in items['two'].cands] == [right['filename']]
        assert items['two'].want == Want(200) and items['two'].checked
    finally:
        await client.close()
