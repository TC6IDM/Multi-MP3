"""Download each track once, however many playlists it's in.

A track already downloaded for one playlist is *copied* into the next playlist
that contains it, instead of being fetched again — no request, no rate-limit
budget, no waiting. It is a copy rather than a hard link on purpose: SoundCloud
and YouTube files carry the playlist in their tags (album = playlist name,
track number = position), so each playlist's copy gets its own tags. A hard
link would share one set of tags between both playlists, and whichever was
tagged last would be wrong in the other when imported into Apple Music.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
from pathlib import Path

from src.state import DONE, StateStore

_LEADING_NUMBER = re.compile(r"^\s*\d+\s*")


class DoneIndex:
    """(provider, track id) → a downloaded file, across every playlist record."""

    def __init__(self, store: StateStore) -> None:
        self._lock = threading.Lock()
        self._files: dict[tuple[str, str], Path] = {}
        for st in store.all():
            for t in st.ordered():
                if t.get("status") != DONE or not t.get("id"):
                    continue
                path = st.file_path(t)
                if path is not None and path.is_file():
                    self._files.setdefault((st.provider, str(t["id"])), path)

    def find(self, provider: str, track_id: str | None, not_in: Path | None = None) -> Path | None:
        """A copy of this track that isn't already in `not_in` (the target folder)."""
        if not track_id:
            return None
        with self._lock:
            path = self._files.get((provider, str(track_id)))
        if path is None or not path.is_file():
            return None
        if not_in is not None and path.parent.resolve() == not_in.resolve():
            return None
        return path

    def add(self, provider: str, track_id: str | None, path: Path) -> None:
        if track_id:
            with self._lock:
                self._files.setdefault((provider, str(track_id)), path)

    def __len__(self) -> int:
        return len(self._files)


def renumber(name: str, number: int | None, width: int) -> str:
    """The same filename with this playlist's position in front of it."""
    rest = _LEADING_NUMBER.sub("", name, count=1)
    if number is None or width <= 0:
        return rest
    return f"{number:0{width}d} {rest}"


def copy_track(src: Path, dst: Path, *, album: str | None = None,
               album_artist: str | None = None, track: int | None = None) -> Path:
    """Copy a downloaded track into place, retagging it for its new playlist.

    Written to a temporary name and swapped in, so an existing (stale) file at
    the destination is only replaced by a complete copy.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.copying")
    shutil.copy2(src, tmp)
    try:
        if src.suffix.lower() == ".mp3" and (album or album_artist or track):
            _retag(tmp, album=album, album_artist=album_artist, track=track)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def _retag(path: Path, *, album: str | None, album_artist: str | None, track: int | None) -> None:
    from mutagen.id3 import ID3, TALB, TPE2, TRCK, ID3NoHeaderError

    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    if album:
        tags.setall("TALB", [TALB(encoding=3, text=album)])
    if album_artist:
        tags.setall("TPE2", [TPE2(encoding=3, text=album_artist)])
    if track:
        tags.setall("TRCK", [TRCK(encoding=3, text=str(track))])
    tags.save(path)
