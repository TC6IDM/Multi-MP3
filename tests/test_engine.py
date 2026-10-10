"""The yt-dlp engine's queue, record-keeping and failure handling, offline.

yt-dlp itself is replaced by a fake that writes files, so these tests cover
what the engine decides — what to skip, what to retry, what to record — not
whether SoundCloud is up.
"""

import io
import threading
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError
from yt_dlp.utils import DownloadCancelled, DownloadError, ExtractorError

from src.downloaders.ytdlp_engine import EngineProfile, YtDlpEngine, classify
from src.models import CODE_CANCELLED, CODE_OK, CODE_PARTIAL
from src.ratelimit import AdaptiveRateLimiter
from src.state import DONE, PENDING, UNAVAILABLE, StateStore, playlist_key
from src.tui import DownloadProgress

LINK = "https://soundcloud.com/someone/sets/test-set"


# ── classify() ────────────────────────────────────────────────────────

def http_error(status: int, url: str = "https://api-v2.soundcloud.com/tracks/1",
               headers: dict | None = None) -> DownloadError:
    """Shaped like the real thing: DownloadError → ExtractorError → HTTPError."""
    resp = Response(io.BytesIO(b""), url, headers or {}, status=status)
    cause = HTTPError(resp)
    ee = ExtractorError(f"Unable to download JSON metadata: HTTP Error {status}", cause=cause)
    return DownloadError(f"ERROR: [soundcloud] 1: {ee}", exc_info=(type(ee), ee, None))


@pytest.mark.parametrize("exc, kind", [
    (http_error(429), "rate_limited"),
    (http_error(403), "forbidden"),
    (http_error(401), "forbidden"),
    (http_error(404), "unavailable"),
    (DownloadError("ERROR: [youtube] x: Sign in to confirm you’re not a bot"), "rate_limited"),
    (DownloadError("ERROR: [youtube] x: Video unavailable. This video is private"), "unavailable"),
    (DownloadError("ERROR: [youtube] x: Requested format is not available"), "error"),
    (DownloadError("ERROR: Postprocessing: ffmpeg exited with code 1"), "error"),
    (DownloadCancelled("Cancelled"), "cancelled"),
])
def test_classify(exc, kind):
    assert classify(exc).kind == kind


def test_classify_reads_retry_after():
    f = classify(http_error(429, headers={"Retry-After": "42"}))
    assert f.kind == "rate_limited" and f.retry_after == 42


# ── Engine flow with a fake yt-dlp ────────────────────────────────────

class FakeYdl:
    """Stands in for a worker's YoutubeDL: 'downloads' by writing a file."""

    def __init__(self, root: Path, behaviour: dict[str, list[Any]]) -> None:
        self.root = root
        self.behaviour = behaviour      # track id → queue of exceptions to raise first
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def process_ie_result(self, entry, download=True, extra_info=None):
        tid = entry["id"]
        with self.lock:
            self.calls.append(tid)
            pending = self.behaviour.get(tid) or []
            exc = pending.pop(0) if pending else None
        if exc is not None:
            raise exc
        folder = self.root / extra_info["playlist"]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{extra_info['playlist_index']:04d} U - {entry['title']}.mp3"
        path.write_bytes(b"mp3")
        return {"title": entry["title"], "requested_downloads": [{"filepath": str(path)}]}


def playlist_info(n: int) -> dict:
    return {
        "_type": "playlist", "id": "p1", "title": "Test Set", "uploader": "someone",
        "webpage_url": LINK, "extractor": "soundcloud:set", "extractor_key": "SoundcloudSet",
        "entries": [{"_type": "url", "url": f"https://api-v2.soundcloud.com/tracks/{i}",
                     "id": str(i), "title": f"T{i}"} for i in range(1, n + 1)],
    }


def make_engine(tmp_path, fake, n=5, cancel=None, workers=2):
    limiter = AdaptiveRateLimiter("soundcloud", rate=6000, min_rate=1, max_rate=6000,
                                  burst=100, cooldowns=(0.05,))
    profile = EngineProfile(provider="soundcloud", label="SoundCloud", ydl_args=[],
                            outtmpl="%(playlist)s/%(playlist_index)04d %(title)s.%(ext)s",
                            workers=workers, limiter=limiter, max_attempts=2,
                            forbidden_is_rate_limit=True, legacy_archive=True)
    store = StateStore(tmp_path)
    engine = YtDlpEngine(profile, tmp_path, _quiet_logger(), store, DownloadProgress(enabled=False),
                         cancel or threading.Event())
    engine._resolve = lambda pl: (playlist_info(n), "Test Set")  # type: ignore[method-assign]
    engine._ydl = lambda: fake                                    # type: ignore[method-assign]
    fake.root = engine.staging     # like the real one: workers download into staging
    return engine, store


def _quiet_logger():
    import logging
    log = logging.getLogger("test-engine")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def test_downloads_everything_and_records_it(tmp_path):
    fake = FakeYdl(tmp_path, {})
    engine, store = make_engine(tmp_path, fake)
    [result] = engine.run([LINK])
    assert result.code == CODE_OK
    s = store.open("soundcloud", LINK).summary()
    assert (s["done"], s["total"]) == (5, 5)
    assert (tmp_path / "Test Set" / "0003 U - T3.mp3").is_file()


def test_rate_limited_track_is_retried_not_failed(tmp_path):
    fake = FakeYdl(tmp_path, {"3": [http_error(429)]})
    engine, store = make_engine(tmp_path, fake)
    [result] = engine.run([LINK])
    assert result.code == CODE_OK
    assert fake.calls.count("3") == 2
    assert engine.p.limiter.consecutive_hits == 1


def test_unavailable_is_recorded_and_not_retried(tmp_path):
    fake = FakeYdl(tmp_path, {"5": [http_error(404)]})
    engine, store = make_engine(tmp_path, fake)
    [result] = engine.run([LINK])
    assert result.code == CODE_OK          # nothing left a re-run could fetch
    assert fake.calls.count("5") == 1
    assert store.open("soundcloud", LINK).track("5")["status"] == UNAVAILABLE


def test_persistent_error_is_recorded_failed_after_max_attempts(tmp_path):
    boom = DownloadError("ERROR: ffmpeg exploded")
    fake = FakeYdl(tmp_path, {"2": [boom, boom, boom]})
    engine, store = make_engine(tmp_path, fake)
    [result] = engine.run([LINK])
    assert result.code == CODE_PARTIAL
    assert fake.calls.count("2") == 2       # max_attempts
    t = store.open("soundcloud", LINK).track("2")
    assert t["status"] == "failed" and "ffmpeg" in t["error"]


def test_second_run_only_fetches_what_is_left(tmp_path):
    boom = DownloadError("ERROR: ffmpeg exploded")
    first = FakeYdl(tmp_path, {"2": [boom, boom]})
    engine, _ = make_engine(tmp_path, first)
    engine.run([LINK])

    second = FakeYdl(tmp_path, {})
    engine2, store2 = make_engine(tmp_path, second)
    [result] = engine2.run([LINK])
    assert second.calls == ["2"]            # done tracks cost nothing
    assert result.code == CODE_OK
    assert store2.open("soundcloud", LINK).summary()["done"] == 5


def test_deleted_file_is_fetched_again(tmp_path):
    engine, _ = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine.run([LINK])
    (tmp_path / "Test Set" / "0004 U - T4.mp3").unlink()

    again = FakeYdl(tmp_path, {})
    engine2, _ = make_engine(tmp_path, again)
    engine2.run([LINK])
    assert again.calls == ["4"]


def test_reset_redownloads_everything(tmp_path):
    engine, store = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine.run([LINK])
    StateStore(tmp_path).reset()

    again = FakeYdl(tmp_path, {})
    engine2, _ = make_engine(tmp_path, again)
    engine2.run([LINK])
    assert sorted(again.calls) == ["1", "2", "3", "4", "5"]


def test_reset_keeps_old_file_when_redownload_is_cancelled(tmp_path):
    # Regression: yt-dlp told to overwrite deletes the old file before it
    # starts, so a cancelled re-download after a reset used to lose it.
    engine, _ = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine.run([LINK])
    old = tmp_path / "Test Set" / "0002 U - T2.mp3"
    old.write_bytes(b"the original")
    StateStore(tmp_path).reset()

    cancel = threading.Event()

    class CancelOnTwo(FakeYdl):
        def process_ie_result(self, entry, download=True, extra_info=None):
            if entry["id"] == "2":
                # Partway through: a partial file exists in staging, then Cancel.
                (self.root / "Test Set").mkdir(parents=True, exist_ok=True)
                (self.root / "Test Set" / "0002 U - T2.mp3.part").write_bytes(b"par")
                cancel.set()
                raise DownloadCancelled("Cancelled")
            return super().process_ie_result(entry, download, extra_info)

    engine2, store2 = make_engine(tmp_path, CancelOnTwo(tmp_path, {}), cancel=cancel, workers=1)
    engine2.run([LINK])
    assert old.read_bytes() == b"the original"
    assert store2.open("soundcloud", LINK).track("2")["status"] == PENDING
    assert not engine2.staging.exists()                    # debris cleaned up
    assert not list((tmp_path / "Test Set").glob("*.part"))


def test_successful_redownload_replaces_old_file(tmp_path):
    engine, _ = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine.run([LINK])
    old = tmp_path / "Test Set" / "0001 U - T1.mp3"
    old.write_bytes(b"stale")
    StateStore(tmp_path).reset()

    engine2, _ = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine2.run([LINK])
    assert old.read_bytes() == b"mp3"


def test_track_in_two_playlists_is_downloaded_once_and_retagged(tmp_path):
    from mutagen.id3 import ID3

    from src.dedupe import DoneIndex

    engine, store = make_engine(tmp_path, FakeYdl(tmp_path, {}))
    engine.run([LINK])

    other = "https://soundcloud.com/someone/sets/second-set"
    second = playlist_info(3)
    second["title"] = "Second Set"
    second["entries"][0].update(id="5", title="T5")     # already have it as #5 of Test Set
    second["entries"][1].update(id="21", title="T21")   # new
    second["entries"][2].update(id="22", title="T22")   # new
    fake = FakeYdl(tmp_path, {})
    engine2, store2 = make_engine(tmp_path, fake)
    engine2.done_index = DoneIndex(StateStore(tmp_path))
    engine2.p.tags_from_playlist = True
    engine2._resolve = lambda pl: (second, "Second Set")      # type: ignore[method-assign]
    [result] = engine2.run([other])
    assert result.code == CODE_OK
    assert sorted(fake.calls) == ["21", "22"]                 # "5" was copied, not fetched
    copied = tmp_path / "Second Set" / "0001 U - T5.mp3"
    assert copied.is_file()
    tags = ID3(copied)
    assert str(tags["TRCK"]) == "1"
    assert store2.open("soundcloud", other).track("5")["status"] == DONE


def test_first_sight_imports_legacy_archive_and_existing_files(tmp_path):
    key = playlist_key("soundcloud", LINK)
    (tmp_path / ".archive").mkdir()
    (tmp_path / ".archive" / f"{key}.txt").write_text("soundcloud 1\nsoundcloud 2\n", encoding="utf-8")
    (tmp_path / "Test Set").mkdir()
    (tmp_path / "Test Set" / "0003 old - T3.mp3").write_bytes(b"x")

    fake = FakeYdl(tmp_path, {})
    engine, _ = make_engine(tmp_path, fake)
    engine.run([LINK])
    assert sorted(fake.calls) == ["4", "5"]


def test_cancel_before_start_leaves_everything_pending(tmp_path):
    cancel = threading.Event()
    cancel.set()
    fake = FakeYdl(tmp_path, {})
    engine, store = make_engine(tmp_path, fake, cancel=cancel)
    [result] = engine.run([LINK])
    assert result.code == CODE_CANCELLED
    assert fake.calls == []


def test_cancel_mid_track_keeps_it_pending(tmp_path):
    cancel = threading.Event()

    class CancellingYdl(FakeYdl):
        def process_ie_result(self, entry, download=True, extra_info=None):
            if entry["id"] == "2":
                cancel.set()
                raise DownloadCancelled("Cancelled")
            return super().process_ie_result(entry, download, extra_info)

    fake = CancellingYdl(tmp_path, {})
    engine, store = make_engine(tmp_path, fake, cancel=cancel, workers=1)
    [result] = engine.run([LINK])
    assert result.code == CODE_CANCELLED
    st = store.open("soundcloud", LINK)
    assert st.track("1")["status"] == DONE
    assert st.track("2")["status"] == PENDING


def test_lane_gives_up_after_repeated_cooldowns(tmp_path):
    fake = FakeYdl(tmp_path, {"1": [http_error(429) for _ in range(20)]})
    engine, store = make_engine(tmp_path, fake, workers=1)
    engine.p.give_up_after = 2
    [result] = engine.run([LINK])
    assert result.code == CODE_PARTIAL
    st = store.open("soundcloud", LINK)
    assert st.summary()["pending"] >= 1       # recorded as still to do, not failed
    assert st.summary()["failed"] == 0
