import pytest

from songdl import engine, matching, playlist
from songdl.auth import Auth
from songdl.config import ConfigError, _admins
from songdl.errors import UserError
from songdl.server_queue import normalize_folder
from songdl.web import parse_formats, parse_sources


def response(user, *files, free=True, queue=0, speed=1_000_000):
    return {'username': user, 'hasFreeUploadSlot': free, 'queueLength': queue, 'uploadSpeed': speed, 'files': list(files)}


def f(name, size=30_000_000, length=240, **kw):
    return {'filename': name, 'size': size, 'length': length, **kw}


def test_ranking_prefers_quality_among_available_copies():
    path = 'Music\\Daft Punk\\Discovery\\03 - Digital Love.flac'
    rank = matching.rank('daft punk digital love', 'flac', [
        response('a', f(path, bitDepth=16, sampleRate=44100)),
        response('b', f(path, bitDepth=24, sampleRate=96000)),
        response('c', f(path, bitDepth=24, sampleRate=96000), free=False, queue=50),
    ])
    assert [c.username for c in rank.candidates] == ['b', 'a', 'c']


def test_remixes_are_set_aside_unless_asked_for():
    resp = [response('a', f('Digital Love (Some Remix).flac')), response('b', f('Digital Love.flac'))]
    rank = matching.rank('digital love', 'flac', resp)
    assert rank.candidates[0].username == 'b' and rank.variant_files == 1
    assert matching.rank('digital love remix', 'flac', resp).candidates[0].username == 'a'


def test_each_format_only_matches_its_own_extensions():
    resp = [response('a', f('x/Song.wav'), f('x/Song.aif'), f('x/Song.AIFF'), f('x/Song.mp3'), f('x/Song.flac'))]

    def names(fmt):
        return sorted(c.basename for c in matching.rank('song name', fmt, resp).candidates)

    assert names('wav') == ['Song.wav']
    assert names('aiff') == ['Song.AIFF', 'Song.aif']
    assert names('mp3') == ['Song.mp3'] and names('flac') == ['Song.flac']


def test_mp3_is_ranked_by_bitrate_not_by_sample_rate():
    resp = [response('lo', f('a/Song.mp3', bitRate=128, sampleRate=44100)),
            response('hi', f('a/Song.mp3', bitRate=320, sampleRate=44100))]
    best = matching.rank('some song', 'mp3', resp).candidates[0]
    assert best.username == 'hi' and best.quality_label == '320 kbps'


def test_private_and_locked_files_are_skipped():
    rank = matching.rank('some song', 'flac', [response('a', f('PRIVATE/Song.flac'), f('b/Song.flac', isLocked=True))])
    assert not rank.candidates and rank.private_files == 2


def test_candidates_survive_the_queue_file():
    resp = [response('a', f('Artist/Album/01 - Song.flac', bitDepth=24, sampleRate=96000))]
    c = matching.rank('some song', 'flac', resp).candidates[0]
    back = matching.Candidate.from_dict(c.to_dict())
    assert back == c and back.meta['title'] == 'Song'


def test_search_text_adds_the_format():
    assert matching.search_text('Daft Punk - Digital Love', 'aiff') == 'daft punk digital love aiff'


def test_playlist_csv_columns_and_cleanup():
    raw = ('Track Name,Artist Name(s),Album Name,Duration (ms)\n'
           '"Song (feat. X)",A; B,Alb,180000\n,Nobody,,\nSong (feat. X),A,Alb,1\n').encode()
    tracks, skipped = playlist.parse(raw)
    assert [(t.title, t.artist, t.duration) for t in tracks] == [('Song (feat. X)', 'A', 180)]
    assert tracks[0].query == 'A Song' and skipped == 1


@pytest.mark.parametrize('raw, main', [
    ('A; B', 'A'), ('A, B', 'A'), ('Drake, Rihanna', 'Drake'), ('Daft Punk', 'Daft Punk'),
    ('Earth, Wind & Fire', 'Earth, Wind & Fire'), ('Earth, Wind & Fire, The Emotions', 'Earth, Wind & Fire'),
    ('Crosby, Stills, Nash & Young', 'Crosby, Stills, Nash & Young'), ('Peter, Paul and Mary', 'Peter, Paul and Mary'),
    ('Tyler, The Creator', 'Tyler, The Creator'), ('Tyler, The Creator, Kali Uchis', 'Tyler, The Creator'),
    ('Simon & Garfunkel, Someone', 'Simon & Garfunkel'), ('Calvin Harris, Dua Lipa & X', 'Calvin Harris'),
])
def test_main_artist(raw, main):
    assert playlist._main_artist(raw) == main


def test_playlist_without_a_title_column_is_refused():
    with pytest.raises(UserError):
        playlist.parse(b'foo,bar\n1,2\n')


def test_formats_are_validated_and_ordered():
    # Kept in the order given: it's the order they're tried in
    assert parse_formats(['flac', 'WAV']) == ['flac', 'wav']
    assert parse_formats('mp3,aiff,MP3') == ['mp3', 'aiff']
    for bad in ([], ['ogg'], 'x', None):
        with pytest.raises(UserError):
            parse_formats(bad)


@pytest.mark.parametrize('raw,out', [('', ''), (' road trip / 2026 ', 'road trip/2026'), ('a\\b', 'a/b'), ('/x/', 'x')])
def test_folders_are_normalised(raw, out):
    assert normalize_folder(raw) == out


@pytest.mark.parametrize('raw', ['..', 'a/../b', '.hidden', 'a:b', 'a/b/c/d/e',
                                 '1b4e28ba-2fa1-11d2-883f-0016d3cca427', 'x' * 200])
def test_bad_folders_are_refused(raw):
    with pytest.raises(UserError):
        normalize_folder(raw)


def test_audio_headers(tmp_path):
    cases = {
        'flac': b'fLaC' + b'\0' * 20, 'wav': b'RIFF\0\0\0\0WAVEfmt ', 'aiff': b'FORM\0\0\0\0AIFF',
        'mp3': b'\xff\xfb\x90\0',
    }
    for fmt, data in cases.items():
        p = tmp_path / fmt
        p.write_bytes(data)
        assert engine.valid_audio(p, fmt) is True
        assert engine.valid_audio(p, 'wav' if fmt != 'wav' else 'flac') is False
    tagged = tmp_path / 'tagged'
    tagged.write_bytes(b'ID3\x04\0\0\0\0\0\0\xff\xfb\x90\0')
    assert engine.valid_audio(tagged, 'mp3') is True


def test_login_lockout_and_unknown_user():
    auth = Auth({'admin': 'correct-horse-battery'})
    assert auth.check('1.1.1.1', 'nobody', 'correct-horse-battery') is None
    assert auth.check('1.1.1.1', 'admin', 'wrong') is None
    assert auth.check('2.2.2.2', 'admin', 'correct-horse-battery') == 'admin'
    for _ in range(3):
        auth.check('1.1.1.1', 'admin', 'wrong')
    assert auth.retry_after('1.1.1.1') > 0 and auth.retry_after('2.2.2.2') == 0
    token, max_age = auth.create_session('admin', remember=False)
    assert auth.user(token) == 'admin' and max_age is None
    auth.end_session(token)
    assert auth.user(token) is None


def test_admin_passwords_are_checked():
    assert _admins({'a': 'long-enough-1'}) == {'a': 'long-enough-1'}
    for bad in ({'a': 'short'}, {'a': 'change-me-please'}, {'': 'long-enough-1'}, ['a']):
        with pytest.raises(ConfigError):
            _admins(bad)


def test_captcha_is_off_unless_turned_on(tmp_path):
    from pathlib import Path

    from songdl import config
    example = (Path(__file__).resolve().parents[2] / 'config.yaml.example').read_text(encoding='utf-8')
    example = example.replace('YourUsername', 'someone').replace('YourPassword', 'hunter2hunter2')
    example = example.replace('admin: change-me-please', 'admin: a-real-password')
    cases = {'example': example, 'no section': example.split('captcha:')[0],
             'on': example.replace('enabled: False', 'enabled: True')}
    got = {}
    for name, text in cases.items():
        path = tmp_path / f'{name}.yaml'
        path.write_text(text, encoding='utf-8')
        got[name] = config.load(path).captcha
    assert got == {'example': False, 'no section': False, 'on': True}


def test_slskd_yml_shares_and_extra_settings(tmp_path):
    from pathlib import Path

    import yaml

    from songdl import config, setup
    example = (Path(__file__).resolve().parents[2] / 'config.yaml.example').read_text(encoding='utf-8')
    example = example.replace('YourUsername', 'someone').replace('YourPassword', 'hunter2hunter2')
    example = example.replace('admin: change-me-please', 'admin: a-real-password')
    path = tmp_path / 'config.yaml'

    # The example: only the downloads folder is shared, and nothing extra is merged in
    path.write_text(example, encoding='utf-8')
    cfg = config.load(path)
    assert cfg.server_dir is None and cfg.server_folder == cfg.downloads_dir
    written = yaml.safe_load(setup.render(dataclasses_replace(cfg, slskd_dir=tmp_path / 'slskd')).read_text())
    assert written['shares'] == {'directories': ['/app/downloads']}

    custom = example.replace('  server_folder:\n', '  server_folder: /music\n')
    custom = custom.replace('  extra_shares: []\n', '  extra_shares:\n    - "[Movies]/movies"\n    - /shows\n')
    custom = custom.replace('slskd: {}\n', 'slskd:\n  shares:\n    filters: ["\\\\.ini$"]\n    directories: [/nope]\n'
                            '  transfers:\n    upload:\n      slots: 20\n  web:\n    authentication:\n      password: nope\n')
    path.write_text(custom, encoding='utf-8')
    cfg = dataclasses_replace(config.load(path), slskd_dir=tmp_path / 'slskd')
    assert cfg.server_dir == Path('/music') and cfg.extra_shares == ('[Movies]/movies', '/shows')
    written = yaml.safe_load(setup.render(cfg).read_text())
    assert written['shares'] == {'directories': ['/app/downloads', '/music', '[Movies]/movies', '/shows'],
                                 'filters': ['\\.ini$']}
    assert written['transfers'] == {'upload': {'slots': 20}, 'download': {'destination': {'exists': 'rename'}}}
    # Song-DL's own settings win, and the API key and password are kept across runs
    assert written['web']['authentication']['password'] != 'nope'
    key = setup.api_key(cfg)
    assert yaml.safe_load(setup.render(cfg).read_text())['web']['authentication']['api_keys']['songdl']['key'] == key

    path.write_text(custom.replace('extra_shares:\n    - "[Movies]/movies"', 'extra_shares:\n    - 5'), encoding='utf-8')
    with pytest.raises(ConfigError):
        config.load(path)


def dataclasses_replace(cfg, **changes):
    import dataclasses
    return dataclasses.replace(cfg, **changes)


def test_sources_keep_the_order_given():
    assert parse_sources(['YouTube', 'soulseek', 'youtube']) == ['youtube', 'soulseek']
    assert parse_sources('soundcloud,soulseek') == ['soundcloud', 'soulseek']
    assert parse_sources(None) == ['soulseek']
    for bad in ([], ['napster'], ',', 3):
        with pytest.raises(UserError):
            parse_sources(bad)


def test_more_after_follows_the_order_given():
    assert matching.more_after('soulseek', ['youtube', 'soulseek', 'soundcloud'])
    assert not matching.more_after('soundcloud', ['youtube', 'soulseek', 'soundcloud'])
    assert matching.more_after('youtube', ['youtube', 'soundcloud'])
    assert not matching.more_after('youtube', ['soulseek'])
