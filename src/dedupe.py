"""Download each track once, however many playlists it's in.

A track already downloaded for one playlist is *copied* into the next playlist
that contains it, instead of being fetched again — no request, no rate-limit
budget, no waiting. It is a copy rather than a hard link on purpose: every file
carries its playlist position in its tags (the sort title, and for SoundCloud
and YouTube the album and track number too), so each playlist's copy gets its
own tags. A hard link would share one set of tags between both playlists, and
whichever was tagged last would be wrong in the other when imported into Apple
Music.
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

from src.state import DONE, StateStore


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

    def moved(self, old: Path, new: Path) -> None:
        """Follow a file that was renamed or moved (tidying, _removed/)."""
        with self._lock:
            for k, p in self._files.items():
                if p == old:
                    self._files[k] = new

    def __len__(self) -> int:
        return len(self._files)


def copy_track(src: Path, dst: Path, tags: dict[str, str] | None = None) -> Path:
    """Copy a downloaded track into place, retagged for its new playlist.

    Written to a temporary name and swapped in, so an existing (stale) file at
    the destination is only replaced by a complete copy.
    """
    from src.tidy import write_tags

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.copying")
    shutil.copy2(src, tmp)
    try:
        if src.suffix.lower() == ".mp3" and tags:
            write_tags(tmp, tags)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst
