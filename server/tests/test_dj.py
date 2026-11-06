"""DJ mode (dj.py) on real audio: every copy a CDJ plays, at the best quality the original allows.
Needs ffmpeg; skipped without it."""

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from songdl import dj, matching
from songdl.library import Library
from tests.conftest import make_config

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='needs ffmpeg')


def make(path: Path, *args, seconds=3, rate=44100, channels=2, source=None):
    """An audio file from a test tone (or the noise in `source`), written by ffmpeg with `args`."""
    tone = source or f'sine=frequency=1000:sample_rate={rate}:duration={seconds}'
    layout = {1: 'mono', 2: 'stereo', 6: '5.1'}[channels]
    subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'lavfi', '-i', tone,
                    '-af', f'aformat=channel_layouts={layout}', *args, '-y', str(path)], check=True)
    return path


def chunks(path: Path):
    """A WAV's chunks in order: [(id, bytes)]."""
    data = path.read_bytes()
    assert data[:4] == b'RIFF' and data[8:12] == b'WAVE'
    assert struct.unpack('<I', data[4:8])[0] == len(data) - 8
    out, i = [], 12
    while i < len(data):
        cid, size = data[i:i + 4], struct.unpack('<I', data[i + 4:i + 8])[0]
        out.append((cid, data[i + 8:i + 8 + size]))
        i += 8 + size + size % 2
    assert i == len(data)
    return out


def wav_format(path: Path):
    """(format tag, channels, rate, depth, data bytes), checking the layout every CDJ reads:
    RIFF, a 16-byte fmt, then data straight after (the classic 44-byte header)."""
    parts = chunks(path)
    assert [c for c, _ in parts[:2]] == [b'fmt ', b'data']
    fmt = parts[0][1]
    assert len(fmt) == 16
    tag, channels, rate, byte_rate, block, depth = struct.unpack('<HHIIHH', fmt)
    assert block == channels * depth // 8 and byte_rate == rate * block
    assert len(parts[1][1]) % block == 0
    return tag, channels, rate, depth, parts[1][1]


def pcm(path: Path, codec='s16le'):
    """The samples ffmpeg decodes from a file, as raw bytes."""
    return subprocess.run(['ffmpeg', '-nostdin', '-loglevel', 'error', '-i', str(path), '-f', codec, '-'],
                          check=True, capture_output=True).stdout


def convert(src, tmp_path):
    return dj.convert(src, tmp_path / 'out', 'Artist - Song', 'Song', 'Artist', 'Album')


def test_hi_res_flac_becomes_a_24_bit_48_khz_wav_with_a_plain_pcm_header(tmp_path):
    src = make(tmp_path / 'a.flac', '-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24', rate=96000)
    copy = convert(src, tmp_path)
    assert copy.made and copy.path.name == 'Artist - Song.wav' and copy.label == 'WAV 24-bit / 48 kHz'
    tag, channels, rate, depth, data = wav_format(copy.path)
    assert (tag, channels, rate, depth) == (1, 2, 48000, 24)   # 1 is PCM; never 65534 (EXTENSIBLE)
    assert abs(len(data) / (48000 * 6) - 3) < 0.01


def test_88_khz_goes_to_44_1_and_a_cd_quality_file_stays_bit_for_bit(tmp_path):
    copy = convert(make(tmp_path / 'a.wav', '-c:a', 'pcm_s24le', rate=88200), tmp_path)
    assert wav_format(copy.path)[:4] == (1, 2, 44100, 24)

    src = make(tmp_path / 'cd.aiff', '-c:a', 'pcm_s16be', source='anoisesrc=d=2:r=44100:a=0.5')
    copy = dj.convert(src, tmp_path / 'cd', 'x')
    tag, channels, rate, depth, data = wav_format(copy.path)
    assert (tag, channels, rate, depth) == (1, 2, 44100, 16)
    assert data == pcm(src)   # no dither, no resampling: the very same samples


def test_a_24_bit_wav_that_ffmpeg_would_mark_extensible_is_rewritten_plain(tmp_path):
    src = make(tmp_path / 'ext.wav', '-c:a', 'pcm_s24le', rate=48000, source='anoisesrc=d=2:r=48000:a=0.5')
    assert struct.unpack('<H', src.read_bytes()[20:22])[0] == 0xFFFE   # what ffmpeg itself writes
    copy = convert(src, tmp_path)
    tag, _, rate, depth, data = wav_format(copy.path)
    assert (tag, rate, depth) == (1, 48000, 24)
    assert data == pcm(src, 's24le')


def test_float_and_32_bit_become_24_bit_and_8_bit_becomes_16(tmp_path):
    for codec in ('pcm_f32le', 'pcm_s32le', 'pcm_f64le'):
        copy = dj.convert(make(tmp_path / f'{codec}.wav', '-c:a', codec), tmp_path / codec, 'x')
        assert wav_format(copy.path)[:4] == (1, 2, 44100, 24), codec
    copy = dj.convert(make(tmp_path / 'u8.wav', '-c:a', 'pcm_u8'), tmp_path / 'u8', 'x')
    assert wav_format(copy.path)[:4] == (1, 2, 44100, 16)


def test_mono_is_copied_into_both_channels_at_the_same_level(tmp_path):
    src = make(tmp_path / 'mono.flac', '-c:a', 'flac', '-sample_fmt', 's16', channels=1, source='anoisesrc=d=2:r=44100:a=0.5')
    tag, channels, _, depth, data = wav_format(convert(src, tmp_path).path)
    assert (tag, channels, depth) == (1, 2, 16)
    mono = pcm(src)
    left = b''.join(data[i:i + 2] for i in range(0, len(data), 4))
    right = b''.join(data[i + 2:i + 4] for i in range(0, len(data), 4))
    assert left == right == mono


def test_surround_is_mixed_down_to_stereo(tmp_path):
    copy = convert(make(tmp_path / 'six.flac', '-c:a', 'flac', channels=6), tmp_path)
    assert wav_format(copy.path)[1] == 2


def test_a_cbr_mp3_is_handed_out_as_it_is(tmp_path):
    src = make(tmp_path / 'cbr.mp3', '-c:a', 'libmp3lame', '-b:a', '320k')
    copy = convert(src, tmp_path)
    assert (copy.path, copy.made, copy.label) == (src, False, 'MP3, as it is')
    # ...also without LAME's Info header, which old encoders never wrote
    bare = make(tmp_path / 'bare.mp3', '-c:a', 'libmp3lame', '-b:a', '192k', '-write_xing', '0')
    assert dj.mp3_cbr(bare) == 192 and not convert(bare, tmp_path).made


def test_a_vbr_mp3_and_streams_from_youtube_and_soundcloud_become_16_bit_wavs(tmp_path):
    vbr = make(tmp_path / 'vbr.mp3', '-c:a', 'libmp3lame', '-q:a', '2', source='anoisesrc=d=3:r=44100:a=0.3')
    assert dj.mp3_cbr(vbr) is None
    assert wav_format(convert(vbr, tmp_path).path)[:4] == (1, 2, 44100, 16)
    opus = make(tmp_path / 'yt.opus', '-c:a', 'libopus', '-b:a', '128k', rate=48000)
    copy = dj.convert(opus, tmp_path / 'opus', 'x')
    assert copy.label == 'WAV 16-bit / 48 kHz' and wav_format(copy.path)[:4] == (1, 2, 48000, 16)
    aac = make(tmp_path / 'sc.m4a', '-c:a', 'aac', '-b:a', '160k')
    assert wav_format(dj.convert(aac, tmp_path / 'aac', 'x').path)[:4] == (1, 2, 44100, 16)
    # An MP3 at 22 kHz (MPEG-2) is CBR, but not a rate every CDJ takes
    low = make(tmp_path / 'low.mp3', '-c:a', 'libmp3lame', '-b:a', '64k', rate=22050)
    assert wav_format(convert(low, tmp_path).path)[2] == 44100


def test_the_tags_come_after_the_audio(tmp_path):
    copy = dj.convert(make(tmp_path / 'a.flac', '-c:a', 'flac'), tmp_path / 'out', 'x',
                      'Digital Love', 'Daft Punk', 'Discovery – 2001')
    parts = chunks(copy.path)
    assert [c for c, _ in parts] == [b'fmt ', b'data', b'LIST']
    info = parts[2][1]
    assert info[:4] == b'INFO' and b'INAM\x0d\x00\x00\x00Digital Love\x00' in info
    assert b'IART' in info and 'Discovery – 2001'.encode() in info


def test_too_long_for_fat32_gets_16_bits_then_an_mp3(tmp_path, monkeypatch):
    src = make(tmp_path / 'mix.flac', '-c:a', 'flac', '-sample_fmt', 's32', '-bits_per_raw_sample', '24', seconds=10)
    monkeypatch.setattr(dj, 'MAX_BYTES', 10 * 44100 * 2 * 3 - 1000)   # no room for 24 bits, room for 16
    copy = dj.convert(src, tmp_path / 'a', 'x')
    assert wav_format(copy.path)[3] == 16
    monkeypatch.setattr(dj, 'MAX_BYTES', 10 * 44100 * 2 * 2 - 1000)
    copy = dj.convert(src, tmp_path / 'b', 'x')
    assert copy.path.suffix == '.mp3' and copy.label.startswith('MP3 320 kbps CBR')
    assert dj.mp3_cbr(copy.path) == 320


def test_a_damaged_file_is_refused(tmp_path):
    bad = tmp_path / 'bad.flac'
    bad.write_bytes(b'fLaC' + b'\0' * 50_000)
    with pytest.raises(dj.UserError):
        dj.convert(bad, tmp_path / 'out', 'x')
    assert not any((tmp_path / 'out').glob('*')) if (tmp_path / 'out').exists() else True


def test_predictions_for_the_result_card():
    def cand(fmt, **kw):
        return matching.Candidate('u', f'a.{fmt}', 1, 200, kw.get('depth'), kw.get('rate'), True, 0, 0, fmt,
                                  vbr=kw.get('vbr', False))

    assert dj.predict(cand('flac', depth=24, rate=96000)) == 'WAV 24-bit / 48 kHz'
    assert dj.predict(cand('wav', depth=16, rate=44100)) == 'WAV 16-bit / 44.1 kHz'
    assert dj.predict(cand('mp3', vbr=True, rate=44100)) == 'WAV 16-bit / 44.1 kHz'
    assert dj.predict(cand('mp3', rate=44100)) == 'MP3 as it is if CBR, else WAV 16-bit'
    assert dj.predict(cand('opus')) == 'WAV 16-bit / 48 kHz'


async def test_a_file_is_converted_once_and_the_copy_is_never_an_original(tmp_path, monkeypatch):
    class F:
        url = 'http://x'
    cfg = make_config(tmp_path, F)
    lib = Library(cfg)
    conv = dj.Converter(cfg, lib)
    folder = cfg.downloads_dir / '0f0f0f0f-0000-4000-8000-000000000000'
    folder.mkdir(parents=True)
    src = make(folder / '01 - Digital Love.flac', '-c:a', 'flac', '-metadata', 'artist=Daft Punk',
               '-metadata', 'title=Digital Love', source='anoisesrc=d=4:r=44100:a=0.5')   # over 200 KB
    first = await conv.copy(src, 'Daft Punk - Digital Love', 'Digital Love', 'Daft Punk')
    assert first.made and first.path.parent.parent == cfg.downloads_dir

    calls = []
    monkeypatch.setattr(dj, 'convert', lambda *a: calls.append(a))
    again = await conv.copy(src, 'Daft Punk - Digital Love', 'Digital Love', 'Daft Punk')
    assert again.path == first.path and not calls
    # A hard link to the same file is the same file
    (cfg.downloads_dir / '1f0f0f0f-0000-4000-8000-000000000000').mkdir()
    link = cfg.downloads_dir / '1f0f0f0f-0000-4000-8000-000000000000' / src.name
    link.hardlink_to(src)
    assert (await conv.copy(link, 'x')).path == first.path and not calls

    # Searches never get the CDJ copy, only the original (once it's written down)
    assert lib.find('daft punk digital love', ['wav', 'flac']) == []
    await lib.refresh()
    found = lib.find('daft punk digital love', ['wav', 'flac'])
    assert found and all(not c.filename.endswith('.wav') for c in found)
    # ...and that holds after a restart
    lib.save()
    fresh = Library(cfg)
    fresh.load()
    assert fresh.dj_copy(dj.source_key(src))[0] == first.path
    await fresh.refresh()
    assert all(not c.filename.endswith('.wav') for c in fresh.find('daft punk digital love', ['wav', 'flac']))


def test_the_copy_keeps_the_source_files_own_tags_and_its_track_number(tmp_path):
    src = make(tmp_path / 'a.flac', '-c:a', 'flac', '-metadata', 'artist=Bruce Springstein',
               '-metadata', 'title=Glory Days', '-metadata', 'album=Born In The U.S.A',
               '-metadata', 'track=10/12', '-metadata', 'date=1984-06-04',
               '-metadata', 'album_artist=1984')
    copy = dj.convert(src, tmp_path / 'out', 'x', 'Glory Days', 'Bruce Springstein Albums', 'Wrong')
    info = dict(chunks(copy.path))[b'LIST']
    assert b'IART\x12\x00\x00\x00Bruce Springstein\x00' in info and b'Albums' not in info
    assert b'Wrong' not in info and b'Born In The U.S.A' in info
    assert b'ITRK\x03\x00\x00\x0010\x00' in info and b'ICRD\x05\x00\x00\x001984\x00' in info
