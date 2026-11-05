"""Picks the best file for a query out of Soulseek search responses.

Formats are WAV, AIFF, FLAC and MP3; each is searched and ranked on its own.

Ranking, best first:
  1. Relevance: the file looks like the song asked for. Remixes, live takes,
     edits and so on are only relevant when the query asks for them, and the
     length has to agree with what most other copies say.
  2. Availability: the uploader has a free slot or a short queue.
  3. Quality: bit depth, then sample rate for lossless files; bitrate for MP3
     (or any file without a bit depth and sample rate). Anything past
     24-bit / 192 kHz is almost always an upsampled fake, so it ranks as CD
     quality.
  4. Speed: free slot, shorter queue, faster upload speed.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

from . import meta

FORMATS = ('wav', 'aiff', 'flac', 'mp3')   # the order the site lists them in
EXTENSIONS = {'wav': ('.wav',), 'aiff': ('.aiff', '.aif'), 'flac': ('.flac',), 'mp3': ('.mp3',)}
LABELS = {'wav': 'WAV', 'aiff': 'AIFF', 'flac': 'FLAC', 'mp3': 'MP3'}
LOSSY = {'mp3'}

# Words that mark a different version of a song than the one usually wanted.
VARIANT_WORDS = {
    'remix', 'remixes', 'rmx', 'mix', 'edit', 'live', 'instrumental', 'inst', 'karaoke', 'acapella',
    'acappella', 'cappella', 'dub', 'cover', 'covers', 'demo', 'rehearsal', 'reprise', 'remake',
    'sped', 'slowed', 'reverb', 'nightcore', 'tribute', 'medley', 'extended', 'vip', 'bootleg',
    'mashup', 'rework', 'piano', 'orchestral', 'acoustic', 'unplugged',
}
# Words that look like variants but describe the normal song.
NEUTRAL_PHRASES = re.compile(r'\b(album|original|lp|single|main)\s+(version|mix)\b')

PRIVATE = re.compile(r'\bprivate\b', re.I)

DURATION_TOLERANCE = 4  # seconds


def tokens(text):
    return re.findall(r'[^\W_]+', text.lower())


def search_text(query, fmt):
    """Query sent to Soulseek. Punctuation is dropped so a leading '-' can't
    exclude terms, and the extension narrows results to the wanted format."""
    words = [w for w in tokens(query) if w != fmt]
    return ' '.join(words + [fmt])


@dataclass
class Candidate:
    username: str
    filename: str
    size: int
    length: int | None
    bit_depth: int | None
    sample_rate: int | None
    free_slot: bool
    queue: int
    speed: int
    fmt: str
    bitrate: int | None = None   # as reported by the uploader, kbps
    vbr: bool = False
    variant: bool = False
    outlier: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def basename(self):
        return self.filename.replace('\\', '/').rsplit('/', 1)[-1]

    @property
    def ext(self):
        return '.' + self.basename.rsplit('.', 1)[-1].lower() if '.' in self.basename else ''

    @property
    def kbps(self):
        if self.bitrate and self.fmt in LOSSY:
            return self.bitrate
        return round(self.size * 8 / self.length / 1000) if self.length else None

    @property
    def relevance(self):
        return 0 if self.variant else 1 if self.outlier else 2

    @property
    def available(self):
        return self.free_slot or self.queue <= 10

    @property
    def quality(self):
        if self.fmt not in LOSSY and (self.bit_depth or self.sample_rate):
            depth, rate = self.bit_depth or 16, self.sample_rate or 44100
            if depth > 24 or rate > 192000:
                depth, rate = 16, 44100
            return (depth, rate, 0)
        return (0, 0, self.kbps or 0)

    @property
    def sort_key(self):
        return (self.relevance, self.available, self.quality, self.free_slot, -self.queue, self.speed)

    @property
    def quality_label(self):
        if self.fmt not in LOSSY and self.bit_depth and self.sample_rate:
            return f'{self.bit_depth}-bit / {self.sample_rate / 1000:g} kHz'
        if self.kbps:
            return f'{self.kbps:,} kbps' + (' VBR' if self.vbr and self.fmt in LOSSY else '')
        return 'Unknown quality'

    def describe(self):
        return f'{self.quality_label} {LABELS[self.fmt]} from {self.username}'

    def availability(self):
        slot = 'free slot' if self.free_slot else f'{self.queue} in queue'
        return f'{slot}, {self.speed / 1e6:.1f} MB/s'

    def to_result(self, result_id):
        return {
            'id': result_id,
            **self.meta,
            'filename': self.filename,
            'format': LABELS[self.fmt],
            'duration': self.length,
            'bitrate': self.kbps,
            'sampleRate': self.sample_rate,
            'bitDepth': self.bit_depth,
            'quality': self.quality_label,
            'size': self.size,
        }

    # What is written to the queue file, so a queued song survives a restart
    def to_dict(self):
        return {
            'username': self.username, 'filename': self.filename, 'size': self.size, 'length': self.length,
            'bit_depth': self.bit_depth, 'sample_rate': self.sample_rate, 'free_slot': self.free_slot,
            'queue': self.queue, 'speed': self.speed, 'fmt': self.fmt, 'bitrate': self.bitrate,
            'vbr': self.vbr, 'variant': self.variant, 'outlier': self.outlier, 'meta': self.meta,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Ranking:
    candidates: list  # best first
    format_files: int = 0
    private_files: int = 0
    variant_files: int = 0
    outlier_files: int = 0


def rank(query, fmt, responses):
    exts = EXTENSIONS[fmt]
    wanted = set(tokens(query)) - {fmt}
    out = Ranking([])

    for resp in responses:
        out.private_files += resp.get('lockedFileCount') or 0
        for f in resp.get('files') or []:
            name = f.get('filename') or ''
            if not name.lower().endswith(exts):
                continue
            out.format_files += 1
            if f.get('isLocked') or PRIVATE.search(name):
                out.private_files += 1
                continue
            size = f.get('size') or 0
            length = f.get('length')
            if size < 200_000 or (length is not None and length < 20):
                continue
            c = Candidate(
                username=resp['username'],
                filename=name,
                size=size,
                length=length,
                bit_depth=f.get('bitDepth'),
                sample_rate=f.get('sampleRate'),
                free_slot=bool(resp.get('hasFreeUploadSlot')),
                queue=resp.get('queueLength') or 0,
                speed=resp.get('uploadSpeed') or 0,
                fmt=fmt,
                bitrate=f.get('bitRate'),
                vbr=bool(f.get('isVariableBitRate')),
            )
            c.variant = _is_variant(c.filename, wanted)
            c.meta = meta.parse(c.filename)
            out.candidates.append(c)

    _mark_outliers([c for c in out.candidates if not c.variant])
    out.variant_files = sum(c.variant for c in out.candidates)
    out.outlier_files = sum(c.outlier and not c.variant for c in out.candidates)
    out.candidates.sort(key=lambda c: c.sort_key, reverse=True)
    return out


def _is_variant(filename, wanted):
    parts = filename.replace('\\', '/').split('/')
    # The file name and its folder (e.g. an album called "Remixes")
    text = NEUTRAL_PHRASES.sub(' ', ' '.join(parts[-2:]).lower().rsplit('.', 1)[0])
    return bool((set(tokens(text)) & VARIANT_WORDS) - wanted)


def _mark_outliers(cands):
    """Flags files whose length disagrees with what most copies say."""
    lengths = [c.length for c in cands if c.length]
    if len(lengths) < 3:
        return
    counts = Counter(lengths)
    # The length with the most copies within the tolerance
    consensus = max(counts, key=lambda L: sum(n for l2, n in counts.items() if abs(l2 - L) <= DURATION_TOLERANCE))
    for c in cands:
        if c.length and abs(c.length - consensus) > max(DURATION_TOLERANCE, consensus * 0.02):
            c.outlier = True
