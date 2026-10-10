from mutagen.id3 import ID3, TALB, TIT2, TRCK

from src.dedupe import DoneIndex, copy_track
from src.state import DONE, StateStore


def test_copy_retags_album_and_track_for_the_new_playlist(tmp_path):
    src = tmp_path / "A" / "0012 U - Song.mp3"
    src.parent.mkdir()
    src.write_bytes(b"")
    tags = ID3()
    tags.add(TIT2(encoding=3, text="Song"))
    tags.add(TALB(encoding=3, text="First Playlist"))
    tags.add(TRCK(encoding=3, text="12"))
    tags.save(src)

    dst = copy_track(src, tmp_path / "B" / "(0003) Song.mp3",
                     {"TALB": "Second Playlist", "TRCK": "3", "TSOT": "(0003) Song"})
    copied, original = ID3(dst), ID3(src)
    assert str(copied["TALB"]) == "Second Playlist" and str(copied["TRCK"]) == "3"
    assert str(copied["TIT2"]) == "Song" and str(copied["TSOT"]) == "(0003) Song"
    assert str(original["TALB"]) == "First Playlist"            # untouched
    assert not list((tmp_path / "B").glob(".*copying"))


def test_done_index_finds_tracks_from_other_playlists_only(tmp_path):
    store = StateStore(tmp_path)
    st = store.open("soundcloud", "https://soundcloud.com/a/sets/one")
    st.update_meta(folder="One")
    st.sync_tracks([{"key": "111", "index": 1, "id": "111"}])
    f = tmp_path / "One" / "0001 x.mp3"
    f.parent.mkdir()
    f.write_bytes(b"x")
    st.mark("111", DONE, file=f)

    idx = DoneIndex(StateStore(tmp_path))
    assert idx.find("soundcloud", "111") == f
    assert idx.find("soundcloud", "111", not_in=tmp_path / "One") is None
    assert idx.find("youtube", "111") is None
    f.unlink()
    assert idx.find("soundcloud", "111") is None                # deleted since


def test_done_index_follows_moved_files(tmp_path):
    store = StateStore(tmp_path)
    st = store.open("soundcloud", "https://soundcloud.com/a/sets/one")
    st.update_meta(folder="One")
    st.sync_tracks([{"key": "111", "index": 1, "id": "111"}])
    old = tmp_path / "One" / "0001 U - x.mp3"
    old.parent.mkdir()
    old.write_bytes(b"x")
    st.mark("111", DONE, file=old)
    idx = DoneIndex(StateStore(tmp_path))

    new = old.with_name("(0001) x.mp3")
    old.rename(new)
    idx.moved(old, new)
    assert idx.find("soundcloud", "111") == new
