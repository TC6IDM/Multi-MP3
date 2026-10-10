"""Lossless volume levelling: MP3 frame editing and the loudness plan."""

import shutil
import subprocess

import pytest

from src import loudness
from src.loudness import Loudness, plan_steps, shift_gain

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")


def test_crc16_is_mpegs():
    # CRC-16 with polynomial 0x8005, initial 0xFFFF, no reflection: the
    # standard check value for "123456789" is 0xAEE7.
    assert loudness._crc16(b"123456789") == 0xAEE7


def test_plan_turns_loud_tracks_down_and_quiet_ones_up_within_headroom():
    assert plan_steps(Loudness(-6.8, 3.8), -14) == -5            # loud EDM master
    assert plan_steps(Loudness(-14.2, -1.0), -14) == 0             # already there
    assert plan_steps(Loudness(-20.0, -10.0), -14) == 4            # room to grow
    assert plan_steps(Loudness(-20.0, -2.0), -14) == 0             # would clip


def test_bounds_skip_id3v2_and_id3v1():
    tag = b"ID3\x03\x00\x00\x00\x00\x00\x05" + b"\x00" * 5
    audio = b"\xff\xfb" + b"\x00" * 30
    v1 = b"TAG" + b"\x00" * 125
    assert loudness._audio_bounds(tag + audio + v1) == (15, 15 + len(audio))


def _make_mp3(path, args):
    subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", *args, str(path)], check=True)


@needs_ffmpeg
@pytest.mark.parametrize("args", [
    ["-c:a", "libmp3lame", "-b:a", "128k"],                       # CBR, Info frame
    ["-c:a", "libmp3lame", "-q:a", "2"],                          # VBR, Xing frame
    ["-c:a", "libmp3lame", "-b:a", "64k", "-ar", "22050"],        # MPEG-2
    ["-c:a", "libmp3lame", "-b:a", "96k", "-ac", "1"],            # mono
])
def test_shift_gain_changes_loudness_by_exactly_the_steps(tmp_path, args):
    src = tmp_path / "in.mp3"
    _make_mp3(src, args)
    before = loudness.measure(src)
    buf = bytearray(src.read_bytes())
    assert shift_gain(buf, -4) == -4
    out = tmp_path / "out.mp3"
    out.write_bytes(buf)
    after = loudness.measure(out)
    assert before is not None and after is not None
    assert after.lufs - before.lufs == pytest.approx(-4 * loudness.STEP_DB, abs=0.2)
    errors = subprocess.run([FFMPEG, "-v", "error", "-nostdin", "-i", str(out), "-f", "null", "-"],
                            capture_output=True, text=True).stderr
    assert errors == ""


@needs_ffmpeg
def test_finish_file_levels_once_and_records_it(tmp_path):
    from src.tidy import finish_file

    f = tmp_path / "x.mp3"
    _make_mp3(f, ["-c:a", "libmp3lame", "-b:a", "128k", "-af", "volume=15dB"])
    first = finish_file(f, {"TIT2": "Tone"}, level=True)
    assert first.changed and first.gain is not None and first.gain < 0
    assert loudness.read_gain(f) == first.gain
    m = loudness.measure(f)
    assert m is not None and abs(m.lufs - loudness.DEFAULT_TARGET_LUFS) <= 1.0
    second = finish_file(f, {"TIT2": "Tone"}, level=True)
    assert not second.changed and second.gain == first.gain
