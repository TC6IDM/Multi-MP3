"""Names, tags and folder tidying for Apple Music — offline."""

import os
import time

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from src.state import DONE, PENDING, StateStore
from src.tidy import (
    Finisher,
    clean_title,
    file_name,
    finish_file,
    match_files,
    name_matches,
    plan_for,
    safe_filename,
    tidy_playlist,
    write_tags,
)

LINK = "https://soundcloud.com/someone/sets/test-set"


@pytest.mark.parametrize("raw, uploader, title, artist", [
    ("Song Name", "Artist", "Song Name", None),
    ("Artist - Song Name", "Some Label", "Song Name", "Artist"),
    ("Kanye West - Stronger (Official Music Video)", "KanyeWestVEVO", "Stronger", "Kanye West"),
    ("Song [FREE DL]", "x", "Song", None),
    ("Song (Free Download)", "x", "Song", None),
    ("Song | Free Download", "x", "Song", None),
    ("PREMIERE: Artist - Song", "Label", "Song", "Artist"),
    ("Artist - Song (Extended Mix)", "Label", "Song (Extended Mix)", "Artist"),
    ("A x B - Song (VIP)", "Label", "Song (VIP)", "A x B"),
    ("Song - Radio Edit", "Artist", "Song - Radio Edit", None),
    ("Bangarang - Ekali Remix", "Skrillex", "Bangarang - Ekali Remix", None),
    ("NOTION LIVE AT LOLLAPALOOZA CHICAGO - 2026", "NOTION", "NOTION LIVE AT LOLLAPALOOZA CHICAGO - 2026", None),
    ("NOTION - BE YOUR GIRL DUB.wav", "jsong", "BE YOUR GIRL DUB", "NOTION"),
    ("jigitz - car crash (ROBY flip)", "ROBY", "car crash (ROBY flip)", "jigitz"),
    ("Get Out Now", "x", "Get Out Now", None),
    ("Song (feat. Someone) [Official Audio]", "x", "Song (feat. Someone)", None),
    ("[Official Video]", "x", "[Official Video]", None),          # nothing left: keep it as it was
])
def test_clean_title(raw, uploader, title, artist):
    assert clean_title(raw, uploader) == (title, artist)


def test_file_names_are_numbered_and_windows_safe():
    assert file_name(7, "Song") == "(0007) Song.mp3"
    assert file_name(12345, "Song") == "(12345) Song.mp3"
    assert safe_filename('AC/DC: "Back" <In> Black?. ') == "AC-DC- 'Back' In Black"
    assert file_name(1, "???") == "(0001) Track.mp3"


def _record(tmp_path, provider="soundcloud", link=LINK, n=3, folder="Set"):
    st = StateStore(tmp_path).open(provider, link)
    st.update_meta(name="My Set", folder=folder, album_artist="me")
    st.sync_tracks([{"key": str(i), "index": i, "id": str(i), "title": f"Artist - Song {i}",
                     "artists": ["Label"], "file_index": i} for i in range(1, n + 1)])
    return st


def _mp3(path, data=b"audio", age=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


def test_plan_puts_the_position_in_the_sort_title_not_the_title(tmp_path):
    st = _record(tmp_path)
    plan = plan_for(st, st.track("2") or {})
    assert plan.name == "(0002) Song 2.mp3"
    assert plan.tags == {"TIT2": "Song 2", "TPE1": "Artist", "TALB": "My Set", "TPE2": "me",
                         "TRCK": "2", "TSOT": "(0002) Song 2"}


def test_plan_leaves_spotify_album_tags_alone(tmp_path):
    st = StateStore(tmp_path).open("spotify", "https://open.spotify.com/playlist/x")
    st.update_meta(name="PL", folder="PL")
    st.sync_tracks([{"key": "a", "index": 4, "file_index": 4, "title": "Krazy (Save Me)", "artists": ["Vivace"]}])
    plan = plan_for(st, st.track("a") or {})
    assert plan.tags == {"TIT2": "Krazy (Save Me)", "TSOT": "(0004) Krazy (Save Me)"}
    assert plan.name == "(0004) Krazy (Save Me).mp3"


def test_name_matching_covers_every_naming_scheme():
    from pathlib import Path
    assert name_matches(Path("0005 Some Label - Artist - Song [FREE DL].mp3"), "Artist - Song [FREE DL]")
    assert name_matches(Path("(0005) Song.mp3"), "Artist - Song [FREE DL]")
    assert name_matches(Path("005 Song - Artist.mp3"), "Song")
    assert not name_matches(Path("0005 Label - Other Tune.mp3"), "Artist - Song")
    assert name_matches(Path("0005 anything.mp3"), "")         # unknown title can't rule it out


def test_match_files_by_number_then_title(tmp_path):
    folder = tmp_path / "Set"
    _mp3(folder / "0001 L - Artist - Song 1.mp3")
    _mp3(folder / "0002 L - Artist - Song 3.mp3")               # song 3 used to be #2
    tracks = [{"key": str(i), "index": i, "title": f"Artist - Song {i}"} for i in (1, 2, 3)]
    found = match_files(folder, tracks)
    assert found["1"].name.startswith("0001")
    assert "2" not in found                                     # #2's file is another song
    assert found["3"].name == "0002 L - Artist - Song 3.mp3"    # found by title


def test_tidy_renames_done_files_and_swaps_safely(tmp_path):
    st = _record(tmp_path)
    folder = tmp_path / "Set"
    a = _mp3(folder / "(0001) Song 2.mp3", b"two")              # tracks 1 and 2 swapped places
    b = _mp3(folder / "(0002) Song 1.mp3", b"one")
    c = _mp3(folder / "0003 Label - Artist - Song 3.mp3", b"three")
    st.mark("1", DONE, file=b, save=False)
    st.mark("2", DONE, file=a, save=False)
    st.mark("3", DONE, file=c, save=False)
    st.save()

    report = tidy_playlist(st, since=time.time())
    assert report.renamed == 3
    assert (folder / "(0001) Song 1.mp3").read_bytes() == b"one"
    assert (folder / "(0002) Song 2.mp3").read_bytes() == b"two"
    assert (folder / "(0003) Song 3.mp3").read_bytes() == b"three"
    assert sorted(p.name for p in folder.iterdir()) == ["(0001) Song 1.mp3", "(0002) Song 2.mp3",
                                                       "(0003) Song 3.mp3"]
    assert st.track("1")["file"] == "Set/(0001) Song 1.mp3"


def test_tidy_leaves_new_and_shared_folders_alone(tmp_path):
    st = _record(tmp_path)
    folder = tmp_path / "Set"
    stray = _mp3(folder / "0009 Someone - Unrelated.mp3")
    fresh = _mp3(folder / "0008 Someone - Just Downloaded.mp3", age=0)
    st.save()
    tidy_playlist(st, since=time.time() - 60, shared_folder=True)
    assert stray.exists()
    tidy_playlist(st, since=time.time() - 60)
    assert not stray.exists() and (tmp_path / "_removed" / "Set" / stray.name).exists()
    assert fresh.exists()                                       # written during this run


def test_tidy_never_sweeps_a_file_that_looks_like_a_current_track(tmp_path):
    st = _record(tmp_path)
    keep = _mp3(tmp_path / "Set" / "0002 Label - Artist - Song 2.mp3")   # pending track's old copy
    st.save()
    tidy_playlist(st, since=time.time())
    assert keep.exists()


def test_reset_keeps_the_old_file_on_record(tmp_path):
    st = _record(tmp_path)
    f = _mp3(tmp_path / "Set" / "(0001) Song 1.mp3")
    st.mark("1", DONE, file=f)
    StateStore(tmp_path).reset()
    t = StateStore(tmp_path).open("soundcloud", LINK).track("1")
    assert t["status"] == PENDING and t["file"] == "Set/(0001) Song 1.mp3"


def test_write_tags_keeps_the_sort_title_in_id3v23(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"audio")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Old"))
    tags.add(TPE1(encoding=3, text="Someone"))
    tags.save(f, v2_version=3)

    write_tags(f, {"TIT2": "New", "TSOT": "(0001) New"})
    after = ID3(f, translate=False)
    assert after.version == (2, 3, 0)
    assert str(after["TIT2"]) == "New" and str(after["TSOT"]) == "(0001) New"
    assert str(after["TPE1"]) == "Someone"                      # untouched
    assert f.read_bytes().endswith(b"audio")


def test_finish_file_is_a_no_op_when_nothing_changed(tmp_path):
    f = tmp_path / "x.mp3"
    f.write_bytes(b"audio")
    assert finish_file(f, {"TIT2": "A"}, level=False).changed
    mtime = f.stat().st_mtime_ns
    assert not finish_file(f, {"TIT2": "A"}, level=False).changed
    assert f.stat().st_mtime_ns == mtime


def test_finisher_records_what_it_applied(tmp_path):
    import logging
    st = _record(tmp_path)
    f = _mp3(tmp_path / "Set" / "(0001) Song 1.mp3")
    st.mark("1", DONE, file=f)
    fin = Finisher(logging.getLogger("t"), level=False, workers=1)
    assert fin.submit_playlist(st) == 1
    fin.close()
    t = st.track("1")
    assert t["tagged"] == plan_for(st, t).sig
    assert not fin.needs(st, t)
    assert str(ID3(f)["TSOT"]) == "(0001) Song 1"


def test_a_mangled_title_is_never_written_into_the_file(tmp_path):
    # Before the runner ran with -X utf8, "Inéz" reached the record as "In\ufffdz".
    st = StateStore(tmp_path).open("spotify", "https://open.spotify.com/playlist/x")
    st.update_meta(name="PL", folder="PL")
    st.sync_tracks([{"key": "a", "index": 92, "file_index": 92,
                     "title": "light years (feat. In\ufffdz)", "artists": ["John Summit"]}])
    plan = plan_for(st, st.track("a") or {})
    assert plan.name is None and "TIT2" not in plan.tags
    fixed = plan_for(st, st.track("a") or {}, fallback_title="light years (feat. Inéz)")
    assert fixed.tags["TIT2"] == "light years (feat. Inéz)"


def test_accents_match_however_they_are_encoded():
    import unicodedata
    from pathlib import Path
    decomposed = unicodedata.normalize("NFD", "092 light years (feat. Inéz) - John Summit.mp3")
    assert name_matches(Path(decomposed), "light years (feat. Inéz)")
