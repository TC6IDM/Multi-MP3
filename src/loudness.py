"""Even volume across every track, without re-encoding a single one.

Playlists mix SoundCloud uploads mastered at -6 LUFS with YouTube rips at -16,
so one song blasts and the next is barely there. Apple Music's Sound Check
would even that out, but it works from values it computes on import — files
copied onto the iPhone with CopyTrans don't get them — and Apple Music ignores
ReplayGain tags altogether. So the change has to be in the audio itself.

It's done the way mp3gain does it: every MP3 granule carries an 8-bit "global
gain", and adding one to it makes the decoder scale that granule by 2^(1/4),
i.e. 1.5 dB. Shifting every granule by the same amount turns the whole track up
or down in 1.5 dB steps with no decoding or re-encoding — nothing is lost, and
every player hears the result. The step count goes into a TXXX tag
(MULTIMP3_GAIN), which marks the file as levelled and would let the change be
undone exactly.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

GAIN_TAG = "MULTIMP3_GAIN"
STEP_DB = 20 * math.log10(2 ** 0.25)          # one global_gain step: 1.505 dB
DEFAULT_TARGET_LUFS = -14.0                   # what Spotify and YouTube aim for
# A quiet track is only turned up as far as its loudest sample allows without
# clipping. Turning down never clips.
PEAK_CEILING_DBFS = -1.0

_I_RE = re.compile(r"^\s*I:\s+(-?inf|-?\d+(?:\.\d+)?)\s+LUFS", re.M)
_PEAK_RE = re.compile(r"^\s*Peak:\s+(-?inf|-?\d+(?:\.\d+)?)\s+dBFS", re.M)


@dataclass
class Loudness:
    lufs: float          # integrated loudness (EBU R128)
    peak_dbfs: float     # highest decoded sample


def measure(path: Path, timeout: int = 900) -> Loudness | None:
    """Integrated loudness and sample peak, from ffmpeg's ebur128 filter.

    None for silence, or when ffmpeg is missing or can't decode the file.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path), "-map", "0:a:0",
           # framelog=quiet: otherwise it logs ten lines per second of audio.
           "-af", "ebur128=peak=sample:framelog=quiet", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    lufs, peaks = _I_RE.findall(r.stderr), _PEAK_RE.findall(r.stderr)
    if r.returncode != 0 or not lufs or not peaks:
        return None
    loudness = Loudness(float(lufs[-1]), float(peaks[-1]))
    if not math.isfinite(loudness.lufs) or loudness.lufs < -60:
        return None
    return loudness


def plan_steps(loudness: Loudness, target_lufs: float = DEFAULT_TARGET_LUFS) -> int:
    """How many 1.5 dB steps bring a track to the target (negative = quieter)."""
    steps = round((target_lufs - loudness.lufs) / STEP_DB)
    if steps > 0 and math.isfinite(loudness.peak_dbfs):
        headroom = math.floor((PEAK_CEILING_DBFS - loudness.peak_dbfs) / STEP_DB)
        steps = min(steps, max(0, headroom))
    return steps


# ── MP3 frames ──────────────────────────────────────────────────────────

_BITRATE_KBPS = {
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),   # MPEG-1
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),      # MPEG-2
    0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),      # MPEG-2.5
}
_SAMPLE_RATE = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}
# Below this global gain even the largest quantised value decodes under
# -96 dBFS (8206^(4/3) × 2^((g-210)/4), with headroom for the filterbank), so
# such a granule — a fade-out's tail — can be clamped at 0 without anyone
# hearing it. Counting them would have blocked turning down any track that
# fades to silence: the first real Spotify file tried had gains down to 0.
_INAUDIBLE_BELOW = 56


@dataclass
class _Header:
    version: int      # 3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5
    sample_rate: int
    length: int       # whole frame, in bytes
    mono: bool
    crc: bool         # a 16-bit CRC follows the header

    @property
    def side_len(self) -> int:
        if self.version == 3:
            return 17 if self.mono else 32
        return 9 if self.mono else 17


def _header(buf: bytes | bytearray, i: int) -> _Header | None:
    """A Layer III frame header at `i`, or None."""
    if i + 4 > len(buf) or buf[i] != 0xFF or (buf[i + 1] & 0xE0) != 0xE0:
        return None
    b1, b2, b3 = buf[i + 1], buf[i + 2], buf[i + 3]
    version, layer = (b1 >> 3) & 3, (b1 >> 1) & 3
    if version == 1 or layer != 1:
        return None
    bitrate = _BITRATE_KBPS[version][b2 >> 4]
    rate_index = (b2 >> 2) & 3
    if not bitrate or rate_index == 3:      # free-format or reserved
        return None
    rate = _SAMPLE_RATE[version][rate_index]
    length = (144000 if version == 3 else 72000) * bitrate // rate + ((b2 >> 1) & 1)
    return _Header(version, rate, length, (b3 >> 6) == 3, not (b1 & 1))


def _audio_bounds(buf: bytes | bytearray) -> tuple[int, int]:
    """Where the MPEG frames are: after an ID3v2 tag, before ID3v1/APE tags."""
    start = 0
    if buf[:3] == b"ID3" and len(buf) >= 10:
        size = (buf[6] << 21) | (buf[7] << 14) | (buf[8] << 7) | buf[9]
        start = 10 + size + (10 if buf[5] & 0x10 else 0)
    end = len(buf)
    if end - start >= 128 and buf[end - 128:end - 125] == b"TAG":
        end -= 128
    if end - start >= 32 and buf[end - 32:end - 24] == b"APETAGEX":
        size = int.from_bytes(buf[end - 20:end - 16], "little")
        flags = int.from_bytes(buf[end - 12:end - 8], "little")
        end -= size + (32 if flags & 0x80000000 else 0)
    return start, max(start, end)


def _frames(buf: bytes | bytearray) -> list[tuple[int, _Header]]:
    """Every Layer III frame, walked header to header.

    Anything that doesn't parse is skipped byte by byte until two consecutive
    valid headers of the same stream turn up again, so junk between frames (or
    a header-like byte pattern inside it) can't be mistaken for audio.
    """
    start, end = _audio_bounds(buf)
    frames: list[tuple[int, _Header]] = []
    stream: tuple[int, int] | None = None
    i = start
    while i + 4 <= end:
        h = _header(buf, i)
        ok = h is not None and i + h.length <= end and \
            (stream is None or (h.version, h.sample_rate) == stream)
        if ok and h is not None and (stream is None or not frames or frames[-1][0] + frames[-1][1].length != i):
            # First frame, or re-syncing after junk: the next frame must line up too.
            nxt = _header(buf, i + h.length)
            ok = (nxt is not None and (nxt.version, nxt.sample_rate) == (h.version, h.sample_rate)) \
                or i + h.length >= end
        if not ok or h is None:
            i = buf.find(b"\xff", i + 1, end)
            if i < 0:
                break
            continue
        stream = (h.version, h.sample_rate)
        frames.append((i, h))
        i += h.length
    return frames


def _get(buf: bytearray, bit: int, n: int) -> int:
    byte, shift = bit >> 3, bit & 7
    chunk = int.from_bytes(buf[byte:byte + 3].ljust(3, b"\0"), "big")
    return (chunk >> (24 - shift - n)) & ((1 << n) - 1)


def _put(buf: bytearray, bit: int, n: int, value: int) -> None:
    byte, shift = bit >> 3, bit & 7
    chunk = int.from_bytes(buf[byte:byte + 3], "big")
    pos = 24 - shift - n
    mask = ((1 << n) - 1) << pos
    buf[byte:byte + 3] = ((chunk & ~mask) | (value << pos)).to_bytes(3, "big")


def _crc16(data: bytes | bytearray) -> int:
    """MPEG audio's CRC-16 (polynomial 0x8005, initial value 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x8005) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def _granules(h: _Header, side: int) -> list[int]:
    """Bit offset of each granule/channel's side-info block in the frame."""
    channels = 1 if h.mono else 2
    if h.version == 3:
        first, size, count = 9 + (5 if h.mono else 3) + 4 * channels, 59, 2
    else:
        first, size, count = 8 + (1 if h.mono else 2), 63, 1
    base = side * 8 + first
    return [base + (g * channels + c) * size for g in range(count) for c in range(channels)]


def _is_info_frame(buf: bytearray, i: int, h: _Header, side: int) -> bool:
    """The Xing/Info/VBRI frame encoders put first: silent, and its own CRC
    covers its side info, so it is left exactly as it is."""
    tag = buf[side + h.side_len:side + h.side_len + 4]
    return tag in (b"Xing", b"Info") or buf[i + 36:i + 40] == b"VBRI"


def shift_gain(buf: bytearray, steps: int) -> int:
    """Turn every frame up (or down) by `steps` × 1.5 dB, in place.

    Returns the steps actually applied: fewer when an audible granule's gain
    would run out of its 0–255 range. Inaudible granules (no data, or a gain
    below _INAUDIBLE_BELOW) are clamped instead of holding the track back.
    """
    blocks: list[int] = []
    crc_frames: list[tuple[int, _Header, int]] = []
    lo, hi = 255, 0
    for n, (i, h) in enumerate(_frames(buf)):
        side = i + 4 + (2 if h.crc else 0)
        if n == 0 and _is_info_frame(buf, i, h, side):
            continue
        for bit in _granules(h, side):
            blocks.append(bit)
            if _get(buf, bit, 12):              # part2_3_length: granule has data
                gain = _get(buf, bit + 21, 8)
                hi = max(hi, gain)
                if gain >= _INAUDIBLE_BELOW:
                    lo = min(lo, gain)
        if h.crc:
            crc_frames.append((i, h, side))
    if not blocks:
        return 0
    steps = max(-lo, min(255 - hi, steps))
    if steps == 0:
        return 0
    for bit in blocks:
        gain = _get(buf, bit + 21, 8)
        _put(buf, bit + 21, 8, max(0, min(255, gain + steps)))
    for i, h, side in crc_frames:
        crc = _crc16(bytes(buf[i + 2:i + 4]) + bytes(buf[side:side + h.side_len]))
        buf[i + 4:i + 6] = crc.to_bytes(2, "big")
    return steps


# ── The gain tag ────────────────────────────────────────────────────────

def read_gain(path: Path) -> int | None:
    """Steps already applied to this file, or None if it was never levelled."""
    from mutagen.id3 import ID3, ID3NoHeaderError

    try:
        frames = ID3(path, translate=False).getall(f"TXXX:{GAIN_TAG}")
    except (ID3NoHeaderError, OSError, ValueError):
        return None
    except Exception:
        return None
    for f in frames:
        text = str(f.text[0]) if f.text else ""
        m = re.match(r"\s*([+-]?\d+)", text)
        if m:
            return int(m.group(1))
    return None


def gain_text(steps: int, loudness: Loudness | None) -> str:
    """The tag value: steps applied, then what the file measured beforehand."""
    db = f"{steps * STEP_DB:+.1f} dB"
    if loudness is None:
        return f"{steps:+d} ({db})"
    return f"{steps:+d} ({db}; was {loudness.lufs:.1f} LUFS, peak {loudness.peak_dbfs:.1f} dBFS)"


def enabled() -> bool:
    return os.getenv("LEVEL_VOLUME", "1").strip().lower() not in ("0", "false", "no", "off")


def target_lufs() -> float:
    try:
        return float(os.getenv("LEVEL_TARGET_LUFS", "") or DEFAULT_TARGET_LUFS)
    except ValueError:
        return DEFAULT_TARGET_LUFS


def replace_file(tmp: Path, path: Path, attempts: int = 5) -> None:
    """os.replace, retried: Windows refuses while a player has the file open."""
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.3 * (attempt + 1))
