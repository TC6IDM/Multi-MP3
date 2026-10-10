import json
import threading

from src.state import (
    DONE,
    FAILED,
    PENDING,
    UNAVAILABLE,
    PlaylistState,
    StateStore,
    index_files,
    playlist_key,
    read_legacy_archive,
)

SC_LINK = ("https://soundcloud.com/tc6idm/sets/gym-bangers?si=694aee8a63c74c298eebbf0085c0ee31"
           "&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing")


def _tracks(n):
    return [{"key": str(i), "index": i, "id": str(i), "title": f"Song {i}", "artists": ["A"]}
            for i in range(1, n + 1)]


# ── Keys ──────────────────────────────────────────────────────────────

def test_soundcloud_key_matches_the_legacy_archive_filename():
    # The real archive file this project already has on disk for that link.
    assert playlist_key("soundcloud", SC_LINK) == "tc6idm-sets-gym-bangers-75ea4659"


def test_key_ignores_share_link_noise():
    a = playlist_key("spotify", "https://open.spotify.com/playlist/0SeWUhJeF037ZDWZeBxEZl?si=aaa")
    b = playlist_key("spotify", "https://open.spotify.com/playlist/0SeWUhJeF037ZDWZeBxEZl?si=bbb")
    assert a == b


def test_spotify_key_is_case_sensitive():
    a = playlist_key("spotify", "https://open.spotify.com/playlist/AbCdEf")
    b = playlist_key("spotify", "https://open.spotify.com/playlist/abcdef")
    assert a != b


def test_youtube_key_uses_the_list_id_not_the_watch_path():
    a = playlist_key("youtube", "https://www.youtube.com/watch?v=one&list=PLabc")
    b = playlist_key("youtube", "https://www.youtube.com/watch?v=two&list=PLabc")
    c = playlist_key("youtube", "https://www.youtube.com/watch?v=one&list=PLxyz")
    assert a == b
    assert a != c


# ── Record lifecycle ──────────────────────────────────────────────────

def test_record_round_trips_through_disk(tmp_path):
    store = StateStore(tmp_path)
    st = store.open("soundcloud", SC_LINK)
    st.update_meta(name="GYM BANGERS", folder="GYM BANGERS")
    st.sync_tracks(_tracks(3))
    (tmp_path / "GYM BANGERS").mkdir()
    song = tmp_path / "GYM BANGERS" / "0001 A - Song 1.mp3"
    song.write_bytes(b"x")
    st.mark("1", DONE, file=song)
    st.mark("2", FAILED, error="boom")

    reloaded = StateStore(tmp_path).load("soundcloud", st.key)
    assert reloaded is not None and not reloaded.is_new
    tracks = {t["key"]: t for t in reloaded.ordered()}
    assert tracks["1"]["status"] == DONE
    assert tracks["1"]["file"] == "GYM BANGERS/0001 A - Song 1.mp3"   # relative, portable
    assert tracks["2"]["status"] == FAILED and tracks["2"]["error"] == "boom"
    assert tracks["3"]["status"] == PENDING
    s = reloaded.summary()
    assert (s["done"], s["failed"], s["pending"], s["left"], s["total"]) == (1, 1, 1, 2, 3)


def test_save_leaves_no_temp_file_behind(tmp_path):
    st = StateStore(tmp_path).open("youtube", "https://youtube.com/playlist?list=PL1")
    st.sync_tracks(_tracks(2))
    st.save()
    files = list((tmp_path / ".state" / "youtube").iterdir())
    assert [f.suffix for f in files] == [".json"]
    json.loads(files[0].read_text(encoding="utf-8"))   # complete, valid JSON


def test_sync_keeps_status_and_learned_titles(tmp_path):
    st = StateStore(tmp_path).open("soundcloud", SC_LINK)
    st.sync_tracks(_tracks(2))
    st.mark("1", DONE)
    # A later resolve: track 1 arrives as a title-less stub, track 2 has left
    # the playlist and a new track 3 joined.
    st.sync_tracks([{"key": "1", "index": 1, "id": "1", "title": ""},
                    {"key": "3", "index": 2, "id": "3", "title": "New"}])
    tracks = {t["key"]: t for t in st.ordered()}
    assert set(tracks) == {"1", "3"}
    assert tracks["1"]["status"] == DONE and tracks["1"]["title"] == "Song 1"
    assert tracks["3"]["status"] == PENDING


def test_reset_makes_everything_pending_and_survives_reload(tmp_path):
    store = StateStore(tmp_path)
    st = store.open("soundcloud", SC_LINK)
    st.sync_tracks(_tracks(3))
    for k in ("1", "2"):
        st.mark(k, DONE)
    st.mark("3", UNAVAILABLE, error="404")

    assert store.reset([("soundcloud", st.key)]) == 1

    reloaded = StateStore(tmp_path).load("soundcloud", st.key)
    assert reloaded is not None
    # Still a known record, so the next run won't re-seed it from the MP3s on
    # disk — that would quietly undo the reset.
    assert reloaded.is_new is False
    assert reloaded.data["reset_at"]
    assert {t["status"] for t in reloaded.ordered()} == {PENDING}


def test_reset_all_resets_every_record(tmp_path):
    store = StateStore(tmp_path)
    for link in (SC_LINK, "https://soundcloud.com/x/sets/other"):
        st = store.open("soundcloud", link)
        st.sync_tracks(_tracks(1))
        st.mark("1", DONE)
    assert StateStore(tmp_path).reset() == 2
    assert all(s.summary()["done"] == 0 for s in StateStore(tmp_path).all())


def test_heal_sends_deleted_files_back_to_pending(tmp_path):
    st = StateStore(tmp_path).open("soundcloud", SC_LINK)
    st.sync_tracks(_tracks(2))
    kept, deleted = tmp_path / "a.mp3", tmp_path / "b.mp3"
    kept.write_bytes(b"x")
    deleted.write_bytes(b"x")
    st.mark("1", DONE, file=kept)
    st.mark("2", DONE, file=deleted)
    deleted.unlink()

    assert st.heal() == 1
    assert st.is_done("1") and not st.is_done("2")


def test_concurrent_marks_all_land(tmp_path):
    st = StateStore(tmp_path).open("soundcloud", SC_LINK)
    st.sync_tracks(_tracks(40))

    def worker(keys):
        for k in keys:
            st.mark(k, DONE)

    threads = [threading.Thread(target=worker, args=([str(i) for i in range(n, 41, 4)],))
               for n in range(1, 5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    reloaded = PlaylistState(st.path, tmp_path, "soundcloud", st.key)
    assert reloaded.summary()["done"] == 40


def test_load_rejects_path_tricks(tmp_path):
    store = StateStore(tmp_path)
    assert store.load("soundcloud", "../../etc") is None
    assert store.load("../x", "abc") is None


def test_corrupt_record_is_treated_as_new(tmp_path):
    store = StateStore(tmp_path)
    key = playlist_key("soundcloud", SC_LINK)
    path = store.path_for("soundcloud", key)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.open("soundcloud", SC_LINK).is_new


# ── Migration helpers ─────────────────────────────────────────────────

def test_read_legacy_archive(tmp_path):
    archive = tmp_path / "a.txt"
    archive.write_text("soundcloud 111\nsoundcloud 222\n\n", encoding="utf-8")
    assert read_legacy_archive(archive) == {"111", "222"}
    assert read_legacy_archive(tmp_path / "missing.txt") == set()


def test_index_files_maps_leading_numbers_to_mp3s(tmp_path):
    for name in ("0001 A - x.mp3", "002 B - y.mp3", "notes.txt", "C - no number.mp3", "0003 x.m4a"):
        (tmp_path / name).write_bytes(b"x")
    found = index_files(tmp_path)
    assert sorted(found) == [1, 2]
    assert found[2].name == "002 B - y.mp3"
