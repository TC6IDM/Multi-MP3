"""Spotify through spotdl: the track-list handshake, records, set-aside — offline."""

import logging
import os
import time

from src.coordinator import Coordinator
from src.downloaders.spotify import SpotifyDownloader, link_kind, spotdl_folder_name
from src.models import CODE_OK, CODE_PARTIAL
from src.state import DONE, FAILED, PENDING, StateStore
from src.tui import DownloadProgress

LINK = "https://open.spotify.com/playlist/0SeWUhJeF037ZDWZeBxEZl?si=x"
LOG = logging.getLogger("test-spotify")


def songs(n=3, list_name="PL"):
    """What spotdl_runner.py prints for a playlist."""
    return [{"url": f"https://open.spotify.com/track/id{i}", "song_id": f"id{i}", "name": f"Song {i}",
             "artists": ["A"], "duration": 180, "list_name": list_name, "list_position": i,
             "list_length": n, "track_number": 1, "cover_url": None} for i in range(1, n + 1)]


def old_file(folder, name, age=3600, data=b"old"):
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / name
    p.write_bytes(data)
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_link_kind_and_folder_name():
    assert link_kind("https://open.spotify.com/album/x?si=1") == "album"
    assert link_kind("https://open.spotify.com/intl-de/track/x") == "track"
    assert spotdl_folder_name('AC/DC: "Best" <Of>?') == "ACDC- 'Best' Of"


def test_runner_is_launched_isolated(tmp_path, monkeypatch):
    # Without -I the runner's own folder shadows spotdl's `soundcloud` library
    # with our soundcloud.py, and spotdl dies on import.
    from src.downloaders import spotify as spotify_mod

    monkeypatch.setattr(spotify_mod, "spotdl_command", lambda: "spotdl")
    monkeypatch.setattr(spotify_mod, "spotdl_python", lambda: "py")
    d = SpotifyDownloader(tmp_path, LOG)
    captured = {}
    d._download = lambda name, link, cmd, **kw: captured.setdefault("cmd", cmd) and (0, None)  # type: ignore[method-assign]
    d.download(LINK, on_songs=lambda s: [])
    assert captured["cmd"][:3] == ["py", "-I", str(spotify_mod.SPOTDL_RUNNER)]
    assert "--overwrite" in captured["cmd"] and "force" in captured["cmd"]


def test_tracks_from_songs_numbers_by_position_or_album_track():
    s = songs(2)
    s[1]["track_number"] = 7
    playlist = SpotifyDownloader.tracks_from_songs(s, "playlist")
    album = SpotifyDownloader.tracks_from_songs(s, "album")
    artist = SpotifyDownloader.tracks_from_songs(s, "artist")
    assert [t["file_index"] for t in playlist] == [1, 2]
    assert [t["file_index"] for t in album] == [1, 7]
    assert [t["file_index"] for t in artist] == [None, None]
    assert playlist[0]["duration_ms"] == 180_000 and playlist[0]["url"].endswith("id1")


def test_first_sight_counts_existing_files(tmp_path):
    d = SpotifyDownloader(tmp_path, LOG)
    old_file(tmp_path / "PL", "1 Song 1 - A.mp3")
    tracks = d.tracks_from_songs(songs(), "playlist")
    st = d.prepare_state(StateStore(tmp_path), LINK, "PL", "PL", tracks)
    assert {t["key"]: t["status"] for t in st.ordered()} == {"id1": DONE, "id2": PENDING, "id3": PENDING}
    assert d.skip_urls(st) == ["https://open.spotify.com/track/id1"]


def test_artist_pages_match_files_by_name(tmp_path):
    d = SpotifyDownloader(tmp_path, LOG)
    old_file(tmp_path / "Artist", "Song 2 - A.mp3")
    tracks = d.tracks_from_songs(songs(3, "Artist"), "artist")
    assert list(d.match_files("Artist", tracks)) == ["id2"]


def test_reconcile_marks_fresh_files_done_and_errors_failed(tmp_path):
    d = SpotifyDownloader(tmp_path, LOG)
    tracks = d.tracks_from_songs(songs(), "playlist")
    st = d.prepare_state(StateStore(tmp_path), LINK, "PL", "PL", tracks)
    started = time.time()
    (tmp_path / "PL").mkdir(exist_ok=True)
    (tmp_path / "PL" / "2 Song 2 - A.mp3").write_bytes(b"new")
    errors = tmp_path / "errors.txt"
    errors.write_text("https://open.spotify.com/track/id3 - AudioProviderError: YT-DLP download error\n",
                      encoding="utf-8")
    d.reconcile(st, "PL", tracks, started, errors, completed=True)
    statuses = {t["key"]: t["status"] for t in st.ordered()}
    assert statuses == {"id1": FAILED, "id2": DONE, "id3": FAILED}
    assert "YT-DLP" in st.track("id3")["error"]


def test_reset_then_failed_redownload_keeps_the_old_copy(tmp_path):
    d = SpotifyDownloader(tmp_path, LOG)
    folder = tmp_path / "PL"
    for i in (1, 2, 3):
        old_file(folder, f"{i} Song {i} - A.mp3")
    tracks = d.tracks_from_songs(songs(), "playlist")
    store = StateStore(tmp_path)
    d.prepare_state(store, LINK, "PL", "PL", tracks)
    store.reset()
    st = d.prepare_state(StateStore(tmp_path), LINK, "PL", "PL", tracks)

    assert d.set_aside_stale(st, "PL", tracks) == 3
    assert not list(folder.glob("*.mp3"))                      # out of spotdl's way
    started = time.time()
    (folder / "1 Song 1 - A.mp3").write_bytes(b"fresh")        # only track 1 succeeds
    d.reconcile(st, "PL", tracks, started, None, completed=False)
    assert d.restore_stale(st, "PL", tracks) == 2
    assert (folder / "1 Song 1 - A.mp3").read_bytes() == b"fresh"
    assert (folder / "2 Song 2 - A.mp3").read_bytes() == b"old"
    statuses = {t["key"]: t["status"] for t in st.ordered()}
    assert statuses["id1"] == DONE and statuses["id2"] == PENDING


def test_truncated_file_from_a_killed_run_is_not_done(tmp_path):
    d = SpotifyDownloader(tmp_path, LOG)
    tracks = d.tracks_from_songs(songs(), "playlist")
    st = d.prepare_state(StateStore(tmp_path), LINK, "PL", "PL", tracks)
    started = time.time()
    (tmp_path / "PL").mkdir(exist_ok=True)
    (tmp_path / "PL" / "1 Song 1 - A.mp3").write_bytes(b"\xff\xfb" + b"\x00" * 100)
    d.reconcile(st, "PL", tracks, started, None, completed=False, check_truncation=True)
    assert st.track("id1")["status"] == PENDING
    assert not (tmp_path / "PL" / "1 Song 1 - A.mp3").exists()


# ── The whole flow, with spotdl replaced by a fake ────────────────────

class FakeSpotdl:
    """Plays spotdl_runner.py's part: list the songs, then 'download' the rest."""

    def __init__(self, folder, listing, fail=()):
        self.folder, self.listing, self.fail = folder, listing, set(fail)
        self.downloaded = []

    def __call__(self, link, on_progress=None, on_songs=None):
        skip = set(on_songs(self.listing)) if on_songs else set()
        for s in self.listing:
            if s["url"] in skip or s["song_id"] in self.fail:
                continue
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / f"{s['list_position']} {s['name']} - A.mp3").write_bytes(b"spotdl")
            self.downloaded.append(s["song_id"])
        return 0, self.folder.parent / ".errors" / "none.txt"


def run_spotify(tmp_path, fake):
    coord = Coordinator(tmp_path, LOG, progress=DownloadProgress(enabled=False))
    d = coord._get_spotify()
    d.download = fake            # type: ignore[method-assign]
    d.search_and_download = lambda **kw: False   # type: ignore[method-assign]
    return coord._download_spotify_link(LINK, 1, 1), coord


def test_spotify_run_records_every_track_through_the_handshake(tmp_path):
    fake = FakeSpotdl(tmp_path / "PL", songs())
    result, coord = run_spotify(tmp_path, fake)
    assert result.code == CODE_OK and result.playlist_name == "PL"
    assert fake.downloaded == ["id1", "id2", "id3"]
    s = coord.store.open("spotify", LINK).summary()
    assert (s["done"], s["total"]) == (3, 3)


def test_second_spotify_run_skips_everything_it_has(tmp_path):
    run_spotify(tmp_path, FakeSpotdl(tmp_path / "PL", songs(), fail={"id2"}))
    again = FakeSpotdl(tmp_path / "PL", songs())
    result, _ = run_spotify(tmp_path, again)
    assert again.downloaded == ["id2"]
    assert result.code == CODE_OK


def test_spotify_failure_is_partial_and_recorded(tmp_path):
    result, coord = run_spotify(tmp_path, FakeSpotdl(tmp_path / "PL", songs(), fail={"id3"}))
    assert result.code == CODE_PARTIAL
    assert coord.store.open("spotify", LINK).track("id3")["status"] == FAILED


def test_song_in_two_playlists_is_downloaded_once(tmp_path):
    run_spotify(tmp_path, FakeSpotdl(tmp_path / "PL", songs()))
    other = "https://open.spotify.com/playlist/OTHER"
    listing = songs(2, "Second")
    listing[0].update(url="https://open.spotify.com/track/id3", song_id="id3", name="Song 3")
    listing[1].update(url="https://open.spotify.com/track/id9", song_id="id9", name="Song 9")
    fake = FakeSpotdl(tmp_path / "Second", listing)
    coord = Coordinator(tmp_path, LOG, progress=DownloadProgress(enabled=False))
    coord._get_spotify().download = fake  # type: ignore[method-assign]
    coord._download_spotify_link(other, 1, 1)
    assert fake.downloaded == ["id9"]                          # id3 was copied, not fetched
    copied = tmp_path / "Second" / "1 Song 3 - A.mp3"
    assert copied.read_bytes() == b"spotdl"
