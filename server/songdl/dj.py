"""DJ mode: copies of songs that every Pioneer CDJ / XDJ plays.

What the players take, going by Pioneer's manuals for every model from the
CDJ-900 and CDJ-2000 up to the CDJ-3000 and the XDJs:

- WAV or AIFF: uncompressed 16- or 24-bit integer PCM at 44.1 or 48 kHz. Only
  the CDJ-2000NXS2, CDJ-3000 and OPUS-QUAD go up to 96 kHz, so 48 kHz is the
  ceiling for "any CDJ". No 32-bit float.
- The WAV header has to say plain PCM (format tag 1). A WAVE_FORMAT_EXTENSIBLE
  header (tag 65534) is the cause of the "E-8305 unsupported file" error, and
  ffmpeg writes one for every 24-bit WAV. So ffmpeg only decodes here, to raw
  PCM, and the header is written by this module: RIFF, a 16-byte fmt chunk,
  the data, and only then the tags (a LIST/INFO chunk, which rekordbox reads),
  so a player that stops reading at the audio never sees anything unusual.
- MP3: 32 to 320 kbps at 44.1 or 48 kHz. VBR files seek slowly and drift, so
  only constant bitrate is used.
- The USB stick is FAT32, so no one file may reach 4 GiB.

What each song becomes, keeping all of the quality the original has and
inventing none:

- Lossless (FLAC, WAV, AIFF, ALAC...): WAV at the same bit depth, 16 or 24 (a
  20-bit file is padded to 24, 8-bit to 16, and 32-bit or float is rounded to
  24). Above 48 kHz it is resampled to 44.1 kHz (88.2, 176.4 kHz) or 48 kHz
  (96, 192 kHz), whichever divides evenly, with the SoX resampler at its best.
  A file already like that comes out bit for bit the same, under a clean
  header.
- A constant bitrate MP3 at 44.1 or 48 kHz is already right, and is handed
  out untouched.
- Any other lossy file (a VBR MP3, SoundCloud's AAC, YouTube's Opus) is
  decoded to a 16-bit WAV. Encoding it to MP3 again would lose more; a WAV
  keeps exactly what the decoder gives, and 16 bits is the least a CDJ takes,
  so nothing is dressed up as better than it is.
- Mono is copied into both channels (the same level, unlike a plain upmix,
  which lowers it 3 dB); more than two channels are mixed down to stereo.
- A WAV that would pass the FAT32 limit (a mix of over 4 hours) is made
  16-bit, and failing that a 320 kbps constant bitrate MP3.

Conversions are kept, in a UUID-named folder like any visitor's download, and
written down in the library (library.py) so the same file isn't converted
twice. They are never handed out as originals.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import matching
from .errors import UserError

log = logging.getLogger('songdl')

MAX_BYTES = 4_000_000_000     # FAT32's limit is 4 GiB less a byte; this leaves room to spare
RATES = (44100, 48000)
SLOTS = 2                     # conversions at once
TIMEOUT = 30 * 60
CHUNK = 1024 * 1024
LOSSLESS = {'flac', 'alac', 'wavpack', 'ape', 'tta', 'mlp', 'truehd', 'shorten'}
MP3_CODECS = {'mp3', 'mp3float'}
INFO_MAX = 250                # characters kept of each tag

NO_FFMPEG = 'DJ mode needs ffmpeg on the server, and it isn\'t installed.'
FAILED = 'Couldn\'t make a CDJ copy of this song. The file may be damaged.'
TOO_LONG = 'This one is too long for a FAT32 USB stick, even as a 320 kbps MP3.'


@dataclass
class Plan:
    """What a file becomes: kind 'wav' (with depth, rate), 'mp3' (320k CBR), or 'keep' (as it is)."""
    kind: str
    depth: int = 16
    rate: int = 44100
    channels: int = 2       # the original's
    reason: str = ''        # why it's kept or made what it is, for the log

    @property
    def label(self):
        if self.kind == 'keep':
            return 'MP3, as it is'
        if self.kind == 'mp3':
            return f'MP3 320 kbps CBR / {self.rate / 1000:g} kHz'
        return f'WAV {self.depth}-bit / {self.rate / 1000:g} kHz'

    @property
    def ext(self):
        return '.wav' if self.kind == 'wav' else '.mp3'


@dataclass
class Copy:
    """A CDJ-ready file: `path`, what it is (`label`), and whether it is a new file made from the
    original (`made`) or the original itself."""
    path: Path
    label: str
    made: bool


def target_rate(rate):
    """44.1 or 48 kHz: the original's when it's one of those, else the one it divides into evenly
    (88.2 kHz -> 44.1, 96 -> 48, 22.05 -> 44.1), else 48."""
    if not rate or rate in RATES:
        return rate or 44100
    return 44100 if rate % 11025 == 0 else 48000


def target_depth(bits, is_float=False):
    if is_float or (bits or 16) > 16:
        return 24
    return 16


def predict(cand: matching.Candidate):
    """What DJ mode will most likely make of a copy, going by what its uploader (or the site)
    says about it, for the result card. The file itself decides in the end."""
    fmt = cand.fmt
    if fmt == 'mp3':
        if cand.vbr or (cand.sample_rate and cand.sample_rate not in RATES):
            return 'WAV 16-bit' + _khz(target_rate(cand.sample_rate) if cand.sample_rate else None)
        return 'MP3 as it is if CBR, else WAV 16-bit'
    if fmt in matching.LOSSY:
        rate = 48000 if fmt == 'opus' else target_rate(cand.sample_rate) if cand.sample_rate else None
        return 'WAV 16-bit' + _khz(rate)
    rate = target_rate(cand.sample_rate) if cand.sample_rate else None
    depth = target_depth(cand.bit_depth) if cand.bit_depth else None
    return 'WAV' + (f' {depth}-bit' if depth else '') + _khz(rate)


def _khz(rate):
    return f' / {rate / 1000:g} kHz' if rate else ''


def source_key(path: Path):
    """Which file this is, the same for every hard link to it: (device, inode, size)."""
    st = path.stat()
    return f'{st.st_dev}:{st.st_ino}:{st.st_size}'


# ------------------------------------------------------------------ Reading the original


@lru_cache(maxsize=1)
def _tools():
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        return None
    try:
        conf = subprocess.run([ffmpeg, '-hide_banner', '-buildconf'], capture_output=True, text=True,
                              timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        conf = ''
    return ffmpeg, ffprobe, '--enable-libsoxr' in conf


def available():
    """Whether DJ mode can work here: ffmpeg and ffprobe are installed."""
    return _tools() is not None


def probe(path: Path):
    """The first audio stream and the container, as ffprobe sees them."""
    tools = _tools()
    if not tools:
        raise UserError(NO_FFMPEG, 503)
    res = subprocess.run(
        [tools[1], '-v', 'error', '-select_streams', 'a:0', '-show_streams', '-show_format', '-of', 'json',
         str(path)], capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(res.stdout or '{}')
    except ValueError:
        data = {}
    streams = data.get('streams') or []
    if res.returncode or not streams:
        log.info('ffprobe can\'t read %s: %s', path, (res.stderr or '').strip()[:300])
        raise UserError(FAILED)
    s = streams[0]
    fmt = data.get('format') or {}

    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    duration = fmt.get('duration') or s.get('duration')
    return {
        'codec': s.get('codec_name') or '',
        'sample_fmt': s.get('sample_fmt') or '',
        'rate': num(s.get('sample_rate')),
        'channels': num(s.get('channels')) or 2,
        'bits': num(s.get('bits_per_raw_sample')) or num(s.get('bits_per_sample')),
        'duration': float(duration) if duration not in (None, 'N/A') else None,
        'container': fmt.get('format_name') or '',
        'tags': {k.lower(): str(v) for k, v in {**(s.get('tags') or {}), **(fmt.get('tags') or {})}.items()},
    }


def played(path: Path):
    """How many seconds of audio a file really holds, found by decoding all of it: a header
    can say anything (a 30-second preview has been seen claiming 4:37). None when that
    can't be told here."""
    tools = _tools()
    if not tools:
        return None
    try:
        res = subprocess.run(
            [tools[0], '-nostdin', '-v', 'error', '-progress', 'pipe:1', '-nostats', '-i', str(path),
             '-map', '0:a:0', '-vn', '-sn', '-dn', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    times = re.findall(r'^out_time_(?:us|ms)=(\d+)', res.stdout or '', re.M)
    seconds = int(times[-1]) / 1e6 if times else 0
    return seconds if seconds > 0 else None


def plan(info, path: Path = None):
    """What the file `info` (from probe) describes becomes."""
    codec, channels, rate = info['codec'], info['channels'], info['rate']
    out_rate = target_rate(rate)
    lossless = codec.startswith('pcm_') or codec in LOSSLESS
    if codec in MP3_CODECS and path is not None and 'mp3' in info['container'] and channels <= 2:
        cbr = mp3_cbr(path)
        if cbr and rate in RATES and cbr <= 320:
            return Plan('keep', rate=rate, channels=channels, reason=f'CBR {cbr} kbps')
        reason = f'CBR {cbr} kbps' if cbr else 'VBR'
    else:
        reason = codec
    if lossless:
        is_float = codec.startswith('pcm_f') or info['sample_fmt'] in ('flt', 'fltp', 'dbl', 'dblp')
        bits = info['bits'] or {'s16': 16, 's16p': 16, 'u8': 8, 'u8p': 8}.get(info['sample_fmt'], 24)
        p = Plan('wav', target_depth(bits, is_float), out_rate, channels,
                 f'{codec} {"float" if is_float else bits}-bit {rate} Hz')
    else:
        p = Plan('wav', 16, out_rate, channels, f'lossy {reason} {rate} Hz')
    # FAT32: no file of 4 GiB or more
    if info['duration']:
        for depth in (p.depth, 16):
            if info['duration'] * p.rate * 2 * depth // 8 + 65536 < MAX_BYTES:
                p.depth = depth
                return p
        return Plan('mp3', rate=out_rate, channels=channels, reason='too long for a FAT32 WAV')
    return p


MP3_BITRATES = {  # kbps by bitrate index: MPEG-1 Layer III, MPEG-2/2.5 Layer III
    1: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
MP3_RATES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def mp3_cbr(path: Path):
    """The bitrate (kbps) when every frame of the MP3 has the same one, else None (VBR, or not
    sure). Reads every frame header, since a VBR file needn't say so in a Xing header, and a
    constant bitrate one often has no header at all."""
    data = path.read_bytes()
    i = 0
    if data[:3] == b'ID3' and len(data) >= 10:
        size = data[6] << 21 | data[7] << 14 | data[8] << 7 | data[9]
        i = 10 + size + (10 if data[5] & 0x10 else 0)
    rates, frames, skipped, first = set(), 0, 0, True
    end = len(data) - (128 if data[-128:-125] == b'TAG' else 0)
    while i + 4 <= end:
        b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
        version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
        index, rate_index = b2 >> 4, (b2 >> 2) & 3
        if data[i] != 0xFF or b1 & 0xE0 != 0xE0 or version == 1 or layer != 1 or index in (0, 15) \
                or rate_index == 3:
            if data[i:i + 3] == b'TAG' or data[i:i + 8] == b'APETAGEX':
                break
            i += 1
            skipped += 1
            if skipped > 4096:   # a lot that isn't audio: can't be sure
                return None
            continue
        kbps = MP3_BITRATES[1 if version == 3 else 2][index]
        rate = MP3_RATES[version][rate_index]
        padding = (b2 >> 1) & 1
        length = (144 if version == 3 else 72) * kbps * 1000 // rate + padding
        if first:
            first = False
            head = data[i:i + 64]
            if b'Xing' in head or b'VBRI' in head:
                return None   # says it's VBR
            if b'Info' in head:   # LAME's constant bitrate header frame: no audio in it
                i += length
                continue
        rates.add(kbps)
        if len(rates) > 1:
            return None
        frames += 1
        i += length
    return rates.pop() if frames >= 10 and len(rates) == 1 else None


# ------------------------------------------------------------------ Writing the copy


def convert(src: Path, folder: Path, stem, title='', artist='', album=''):
    """Makes the CDJ-ready copy of src in folder as stem.wav (or .mp3), or returns src itself when
    it's ready as it is. Blocking: run it in a thread. Returns a Copy."""
    info = probe(src)
    p = plan(info, src)
    tags = _tags(info['tags'], title, artist, album)
    if p.kind == 'keep':
        log.info('DJ: %s is ready as it is (%s)', src.name, p.reason)
        return Copy(src, p.label, False)
    ffmpeg, _, soxr = _tools()
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (stem + p.ext)
    part = folder / (stem + p.ext + '.part')
    started = time.monotonic()
    try:
        if p.kind == 'wav':
            _write_wav(ffmpeg, soxr, src, part, p, info, tags)
        else:
            _write_mp3(ffmpeg, soxr, src, part, p, info, tags)
        if part.stat().st_size >= MAX_BYTES:
            raise UserError(TOO_LONG)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    log.info('DJ: %s (%s) -> %s, %s in %.1f s', src.name, p.reason, dest, p.label, time.monotonic() - started)
    return Copy(dest, p.label, True)


def _tags(found, title, artist, album):
    """The tags for the copy: the source file's own (the names a search result carried can be the
    file name's, 'Artist Albums - Song'), the given ones only where the file has none."""
    track = (found.get('track') or found.get('tracknumber') or '').split('/')[0].strip()
    year = (found.get('date') or found.get('year') or '')[:4]
    return {'title': found.get('title') or title, 'artist': found.get('artist') or artist,
            'album': found.get('album') or album, 'track': track if track.isdigit() else '',
            'date': year if year.isdigit() else ''}


def _filters(p: Plan, info, soxr, dither):
    """The ffmpeg audio filters: the channels, then the sample rate and bit depth."""
    out = []
    if p.channels == 1:
        out.append('pan=stereo|c0=c0|c1=c0')
    resample = info['rate'] != p.rate
    if resample or dither:
        opts = [f'osr={p.rate}']
        if resample and soxr:
            opts += ['resampler=soxr', 'precision=28']
        elif resample:
            opts += ['filter_size=64', 'phase_shift=10', 'cutoff=0.97']
        if dither:
            opts.append('dither_method=triangular')
        out.append('aresample=' + ':'.join(opts))
    return out


def _input(ffmpeg, src, p):
    cmd = [ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-i', str(src),
           '-map', '0:a:0', '-vn', '-sn', '-dn', '-map_metadata', '-1']
    if p.channels > 2:
        cmd += ['-ac', '2']
    return cmd


def _write_wav(ffmpeg, soxr, src, part, p: Plan, info, tags):
    # Fewer bits than the source has (or than the decoder gives): dither, so the rounding is noise
    # rather than distortion. 24 bits is far below anything audible, and needs none.
    codec = info['codec']
    is_float = codec.startswith('pcm_f') or info['sample_fmt'] in ('flt', 'fltp', 'dbl', 'dblp')
    exact = ((codec.startswith('pcm_') or codec in LOSSLESS) and not is_float and (info['bits'] or 16) <= 16
             and info['rate'] == p.rate and p.channels <= 2)
    dither = p.depth == 16 and not exact
    cmd = _input(ffmpeg, src, p)
    filters = _filters(p, info, soxr, dither)
    if filters:
        cmd += ['-af', ','.join(filters)]
    raw = 's16le' if p.depth == 16 else 's24le'
    cmd += ['-ar', str(p.rate), '-c:a', 'pcm_' + raw, '-f', raw, 'pipe:1']
    block = 2 * p.depth // 8
    with tempfile.TemporaryFile() as errors, part.open('wb') as out:
        out.write(_wav_header(2, p.rate, p.depth, 0))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        size = 0
        try:
            while chunk := proc.stdout.read(CHUNK):
                out.write(chunk)
                size += len(chunk)
                if size >= MAX_BYTES:
                    proc.kill()
                    raise UserError(TOO_LONG)
            proc.wait(TIMEOUT)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if proc.returncode or size < block:
            errors.seek(0)
            log.warning('DJ: ffmpeg failed on %s: %s', src, errors.read().decode(errors='replace').strip()[:500])
            raise UserError(FAILED)
        if size % block:   # never seen, but a partial frame would shift every sample after it
            out.truncate(len(_wav_header(2, p.rate, p.depth, 0)) + size - size % block)
            out.seek(0, os.SEEK_END)
            size -= size % block
        if size % 2:
            out.write(b'\0')   # chunks start on an even byte
        out.write(_info_chunk(tags))
        total = out.tell()
        out.seek(0)
        out.write(_wav_header(2, p.rate, p.depth, size, total - 8))


def _wav_header(channels, rate, depth, data_size, riff_size=None):
    """The 44 bytes every WAV reader knows: RIFF, a plain PCM fmt chunk (format tag 1), data."""
    block = channels * depth // 8
    return (b'RIFF' + struct.pack('<I', riff_size if riff_size is not None else 36 + data_size) + b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * block, block, depth)
            + b'data' + struct.pack('<I', data_size))


def _info_chunk(tags):
    """A LIST/INFO chunk with the title, artist, album, track and year, which rekordbox reads from a WAV."""
    body = b''
    for key, value in ((b'INAM', tags['title']), (b'IART', tags['artist']), (b'IPRD', tags['album']),
                       (b'ITRK', tags['track']), (b'ICRD', tags['date'])):
        value = ' '.join(str(value or '').split())[:INFO_MAX]
        if not value:
            continue
        data = value.encode('utf-8') + b'\0'
        body += key + struct.pack('<I', len(data)) + data + (b'\0' if len(data) % 2 else b'')
    if not body:
        return b''
    return b'LIST' + struct.pack('<I', 4 + len(body)) + b'INFO' + body


def _write_mp3(ffmpeg, soxr, src, part, p: Plan, info, tags):
    cmd = _input(ffmpeg, src, p)
    filters = _filters(p, info, soxr, False)
    if filters:
        cmd += ['-af', ','.join(filters)]
    for key, value in tags.items():
        if value:
            cmd += ['-metadata', f'{key}={value}']
    cmd += ['-ar', str(p.rate), '-c:a', 'libmp3lame', '-b:a', '320k', '-id3v2_version', '3', '-f', 'mp3',
            '-y', str(part)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    if res.returncode:
        log.warning('DJ: ffmpeg failed on %s: %s', src, (res.stderr or '').strip()[:500])
        raise UserError(FAILED)


# ------------------------------------------------------------------ Sharing conversions


class Converter:
    """Makes CDJ copies, a few at a time. A file asked for twice at once is converted once, and
    one converted before is handed out again while it's still on the server."""

    def __init__(self, cfg, library):
        self.cfg = cfg
        self.library = library
        self.slots = asyncio.Semaphore(SLOTS)
        self.pending = {}   # source_key -> task making it

    async def copy(self, src: Path, stem, title='', artist='', album=''):
        """The CDJ-ready Copy of src, in a UUID-named folder of its own under the downloads folder
        (so it is served and cleaned up like any download), or src itself if it is ready already."""
        key = await asyncio.to_thread(source_key, src)
        made = self.library.dj_copy(key)
        if made:
            path, label = made
            await asyncio.to_thread(_touch, path)
            return Copy(path, label, True)
        task = self.pending.get(key)
        if not task:
            task = asyncio.create_task(self._make(key, src, stem, title, artist, album))
            self.pending[key] = task
            task.add_done_callback(lambda _t: self.pending.pop(key, None))
        # Shielded: one visitor leaving doesn't stop it for the others waiting on it
        return await asyncio.shield(task)

    async def _make(self, key, src, stem, title, artist, album):
        async with self.slots:
            folder = self.cfg.downloads_dir / str(uuid.uuid4())
            try:
                result = await asyncio.to_thread(convert, src, folder, stem, title, artist, album)
            except subprocess.TimeoutExpired:
                raise UserError(FAILED)
            finally:
                if folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()   # nothing made: the file was ready as it was, or it failed
        if result.made:
            self.library.add_dj(result.path, title, artist, album, key, result.label)
        return result


def _touch(path: Path):
    """A copy handed out again starts its clock for deletion over (see web.delete_old_downloads)."""
    try:
        os.utime(path)
    except OSError:
        pass
