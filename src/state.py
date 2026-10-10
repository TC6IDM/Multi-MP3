"""Per-playlist progress records: which tracks are done and which are still to do.

One small JSON file per playlist under downloads/.state/<provider>/. Every
downloader consults it before fetching anything and updates it as each track
finishes, so a run that dies halfway — a rate limit, a crash, Cancel — leaves an
exact record of what is left, and the next run starts from there without
spending a single request on what it already has.

Before this, only SoundCloud had anything like it (yt-dlp download archives of
track ids under .archive/). YouTube and Spotify kept nothing between runs, and
nothing anywhere could say "45 tracks still to go".
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PENDING = "pending"
DONE = "done"
FAILED = "failed"
# Gone from the source (404, private, geo-blocked, a Spotify local file). Not
# retried on later runs — every retry would cost a request for nothing — until
# the playlist is reset.
UNAVAILABLE = "unavailable"
STATUSES = (PENDING, DONE, FAILED, UNAVAILABLE)

_VERSION = 1
# "(0001) Title.mp3" now; "0001 Uploader - Title.mp3" from earlier versions.
_INDEX_RE = re.compile(r"^\s*\(?(\d+)\)?")


def playlist_key(provider: str, link: str) -> str:
    """Stable file-name-safe key for one playlist link.

    Keyed on the part of the URL that names the playlist, so share-link noise
    (?si=, utm_*) and SoundCloud's ?secret_token= don't strand a playlist with
    a second record. For SoundCloud this is byte-for-byte the scheme the old
    .archive/ files used, which is what lets those be imported.
    """
    parsed = urlparse(link)
    if provider == "youtube":
        # watch?v=…&list=… URLs all have the path "watch"; the list id is the
        # only thing that tells two playlists apart.
        query = parse_qs(parsed.query)
        if query.get("list"):
            basis = f"list/{query['list'][0]}"
        elif query.get("v"):
            basis = f"video/{query['v'][0]}"
        else:
            basis = parsed.path.strip("/")
    elif provider == "soundcloud":
        basis = parsed.path.strip("/").lower()
    else:
        # Spotify ids are case-sensitive base62, so no lower-casing here.
        basis = parsed.path.strip("/")
    slug = re.sub(r"[^a-z0-9]+", "-", basis.lower()).strip("-") or "playlist"
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:60]}-{digest}"


def read_legacy_archive(path: Path) -> set[str]:
    """Track ids from a yt-dlp download archive ("soundcloud 123456" lines)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    ids = set()
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            ids.add(parts[-1])
    return ids


def index_files(folder: Path) -> dict[int, Path]:
    """Map each leading track number in a playlist folder to its MP3.

    Every provider names files "<position> …", so the number is how existing
    downloads are matched to playlist entries when there is no record yet. Two
    files with one number (spotdl's fresh "005 …" next to a leftover) resolve
    to the newer one.
    """
    found: dict[int, tuple[float, Path]] = {}
    try:
        entries = list(folder.iterdir())
    except OSError:
        return {}
    for p in entries:
        if p.suffix.lower() != ".mp3" or p.name.startswith("."):
            continue
        m = _INDEX_RE.match(p.name)
        if not m:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        n = int(m.group(1))
        if n not in found or mtime > found[n][0]:
            found[n] = (mtime, p)
    return {n: p for n, (_, p) in found.items()}


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + rename, so a crash never leaves half a record.

    Windows refuses the rename while another process (the web API reading the
    Library, say) has the target open, so that case is retried briefly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


class PlaylistState:
    """The record for one playlist. Thread-safe; every mutation is saved."""

    def __init__(self, path: Path, root: Path, provider: str, key: str, url: str = "") -> None:
        self.path = path
        self.root = root
        self._lock = threading.RLock()
        now = time.time()
        self.data: dict[str, Any] = {
            "version": _VERSION, "provider": provider, "key": key, "url": url,
            "name": "", "folder": "", "total": 0, "reset_at": None,
            "created_at": now, "updated_at": now, "tracks": {},
        }
        # True until a record has been read from disk. A playlist seen for the
        # first time is seeded from whatever is already downloaded; a reset one
        # deliberately is not (see reset()).
        self.is_new = True
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None   # corrupt → treated as new, re-seeded from disk
            if isinstance(loaded, dict) and isinstance(loaded.get("tracks"), dict):
                self.data.update(loaded)
                self.is_new = False
        if url:
            self.data["url"] = url

    # ── Read ────────────────────────────────────────────────────────────

    @property
    def provider(self) -> str:
        return str(self.data.get("provider", ""))

    @property
    def key(self) -> str:
        return str(self.data.get("key", ""))

    @property
    def folder(self) -> str:
        return str(self.data.get("folder", ""))

    def track(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            t = self.data["tracks"].get(key)
            return dict(t, key=key) if t else None

    def ordered(self) -> list[dict[str, Any]]:
        """Tracks in playlist order, each with its key included."""
        with self._lock:
            items = [dict(t, key=k) for k, t in self.data["tracks"].items()]
        return sorted(items, key=lambda t: t.get("index") or 0)

    def file_path(self, track: dict[str, Any]) -> Path | None:
        rel = track.get("file") or ""
        return self.root / rel if rel else None

    def owner_of(self, path: Path) -> str | None:
        """The track whose file this is, if any."""
        rel = self._rel(path)
        with self._lock:
            for k, t in self.data["tracks"].items():
                if t.get("file") == rel:
                    return str(k)
        return None

    def removed(self) -> dict[str, dict[str, Any]]:
        """Tracks that left the source playlist, with where their file went."""
        with self._lock:
            return {k: dict(t) for k, t in (self.data.get("removed") or {}).items()}

    def is_done(self, key: str) -> bool:
        """Done, and (when we know which file it is) that file still exists."""
        t = self.track(key)
        if not t or t.get("status") != DONE:
            return False
        path = self.file_path(t)
        return path is None or path.exists()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = {s: 0 for s in STATUSES}
            for t in self.data["tracks"].values():
                status = t.get("status", PENDING)
                counts[status if status in counts else PENDING] += 1
            total = len(self.data["tracks"])
            return {
                "provider": self.provider, "key": self.key,
                "url": self.data.get("url", ""), "name": self.data.get("name", ""),
                "folder": self.folder, "total": total,
                "done": counts[DONE], "failed": counts[FAILED],
                "unavailable": counts[UNAVAILABLE], "pending": counts[PENDING],
                # "left" is what a re-run would actually try to fetch.
                "left": counts[PENDING] + counts[FAILED],
                "removed": len(self.data.get("removed") or {}),
                "updated_at": self.data.get("updated_at"),
                "reset_at": self.data.get("reset_at"),
            }

    # ── Write ───────────────────────────────────────────────────────────

    def update_meta(self, **fields: Any) -> None:
        with self._lock:
            for k, v in fields.items():
                if v not in (None, ""):
                    self.data[k] = v
            self.data["updated_at"] = time.time()

    def sync_tracks(self, entries: Iterable[dict[str, Any]]) -> list[str]:
        """Make the record match the playlist as it is now. Returns the keys
        of tracks that left it.

        Status carries over for tracks still in the playlist. A track that left
        moves to the record's "removed" list along with its file (which the tidy
        step moves into _removed/), and comes straight back — file and all, no
        download — if it's added again. Titles only ever improve: a resolve
        that comes back without them (SoundCloud stubs) keeps the ones a
        previous run learned.
        """
        entries = list(entries)
        with self._lock:
            old = self.data["tracks"]
            if not entries and old:
                # A playlist doesn't empty itself between runs nearly as often
                # as a resolve comes back blank; don't move a whole folder for it.
                return []
            removed: dict[str, dict[str, Any]] = self.data.setdefault("removed", {})
            fresh: dict[str, dict[str, Any]] = {}
            for e in entries:
                key = str(e["key"])
                t = dict(old.get(key) or removed.pop(key, None) or {
                    "status": PENDING, "file": "", "error": "", "attempts": 0,
                    "title": "", "artists": [], "updated_at": None,
                })
                t.pop("removed_at", None)
                t["index"] = e.get("index")
                if e.get("id"):
                    t["id"] = str(e["id"])
                if e.get("title"):
                    t["title"] = e["title"]
                if e.get("artists"):
                    t["artists"] = list(e["artists"])
                if e.get("local"):
                    t["local"] = True
                # Provider extras: the source URL (Spotify's skip list is by
                # URL), the expected length (spots files cut off mid-write),
                # the number in the filename when it isn't the playlist
                # position (albums use the album track number).
                for extra in ("url", "duration_ms", "file_index"):
                    if e.get(extra) is not None:
                        t[extra] = e[extra]
                fresh[key] = t
            now = time.time()
            gone = [k for k in old if k not in fresh]
            for k in gone:
                # Only tracks with something on disk are worth remembering.
                if old[k].get("file"):
                    removed[k] = dict(old[k], removed_at=now)
            self.data["tracks"] = fresh
            self.data["total"] = len(fresh)
            self.data["updated_at"] = now
            return gone

    def mark(self, key: str, status: str, *, file: Path | str | None = None,
             error: str | None = None, title: str | None = None, save: bool = True,
             **extra: Any) -> None:
        """Set a track's status. `extra` fields (e.g. tagged=, gain=) are
        stored alongside, for a file that was finished before being recorded."""
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._lock:
            t = self.data["tracks"].get(key)
            if t is None:
                return
            t["status"] = status
            if status == DONE:
                t["error"] = ""
                if file:
                    t["file"] = self._rel(file)
                    # A new file: its tags and volume haven't been seen to yet.
                    t.pop("tagged", None)
                    t.pop("gain", None)
            elif status == FAILED:
                t["attempts"] = int(t.get("attempts") or 0) + 1
                t["error"] = (error or "Download failed")[:300]
            elif status == UNAVAILABLE:
                t["error"] = (error or "Not available")[:300]
            elif status == PENDING and error is not None:
                t["error"] = error[:300]
            if title:
                t["title"] = title
            t.update(extra)
            now = time.time()
            t["updated_at"] = now
            self.data["updated_at"] = now
            if save:
                self.save()

    def note(self, key: str, **fields: Any) -> None:
        """Set bookkeeping fields on a track (no status change, not saved).

        `file=` re-points the track at a renamed or moved file.
        """
        with self._lock:
            t = self.data["tracks"].get(key)
            if t is None:
                return
            for k, v in fields.items():
                t[k] = self._rel(v) if k == "file" and v else v

    def note_removed(self, key: str, file: Path) -> None:
        with self._lock:
            t = (self.data.get("removed") or {}).get(key)
            if t is not None:
                t["file"] = self._rel(file)

    def heal(self) -> int:
        """Send done tracks whose file has since been deleted back to pending.

        Replaces the old advice to delete an .archive/ file by hand: deleting
        the MP3 is now enough to have it fetched again.
        """
        healed = 0
        with self._lock:
            for t in self.data["tracks"].values():
                if t.get("status") != DONE:
                    continue
                path = self.file_path(t)
                if path is not None and not path.exists():
                    t["status"] = PENDING
                    t["file"] = ""
                    healed += 1
        return healed

    def reset(self) -> None:
        """Forget every track's progress so the next run re-downloads it all.

        The record itself is kept (with reset_at stamped) rather than deleted:
        a missing record means "first sight" and would be re-seeded from the
        MP3s on disk, quietly undoing the reset. Existing files stay where they
        are and are overwritten track by track as the re-download succeeds —
        which is why each track still remembers its old file: a replacement
        under a new name can then take the old copy's place instead of
        leaving it behind as a duplicate.
        """
        with self._lock:
            for t in self.data["tracks"].values():
                t.update(status=PENDING, error="", attempts=0, updated_at=None)
            now = time.time()
            self.data["reset_at"] = now
            self.data["updated_at"] = now
            self.save()

    def save(self) -> None:
        with self._lock:
            _atomic_write(self.path, json.dumps(self.data, indent=1, ensure_ascii=False))
            self.is_new = False

    def _rel(self, file: Path | str) -> str:
        p = Path(file)
        try:
            return p.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return p.as_posix()


class StateStore:
    """All playlist records under one output directory."""

    def __init__(self, output_dir: Path) -> None:
        self.root = output_dir.resolve()
        self.dir = self.root / ".state"
        self._open: dict[tuple[str, str], PlaylistState] = {}
        self._lock = threading.Lock()

    def path_for(self, provider: str, key: str) -> Path:
        return self.dir / provider / f"{key}.json"

    def legacy_archive(self, key: str) -> Path:
        """Where the pre-records SoundCloud archive for this key lived."""
        return self.root / ".archive" / f"{key}.txt"

    def open(self, provider: str, link: str) -> PlaylistState:
        """The record for a link, shared by everything in this run that asks."""
        key = playlist_key(provider, link)
        with self._lock:
            st = self._open.get((provider, key))
            if st is None:
                st = PlaylistState(self.path_for(provider, key), self.root, provider, key, link)
                self._open[(provider, key)] = st
            return st

    def opened(self) -> list[PlaylistState]:
        """Records touched by this store instance (i.e. this run)."""
        with self._lock:
            return list(self._open.values())

    def load(self, provider: str, key: str) -> PlaylistState | None:
        if not re.fullmatch(r"[a-z0-9-]+", key) or provider not in ("spotify", "youtube", "soundcloud"):
            return None
        path = self.path_for(provider, key)
        if not path.is_file():
            return None
        return PlaylistState(path, self.root, provider, key)

    def all(self) -> list[PlaylistState]:
        states: list[PlaylistState] = []
        if not self.dir.is_dir():
            return states
        for path in sorted(self.dir.glob("*/*.json")):
            st = PlaylistState(path, self.root, path.parent.name, path.stem)
            if not st.is_new:
                states.append(st)
        return states

    def shares_folder(self, st: PlaylistState) -> bool:
        """Does another playlist's record use this playlist's folder too?
        (Two YouTube playlists can share a title.) Tidying then leaves files it
        doesn't know about alone — they may be the other playlist's."""
        folder = st.folder.casefold()
        if not folder:
            return False
        return any(o.folder.casefold() == folder and (o.provider, o.key) != (st.provider, st.key)
                   for o in self.all())

    def reset(self, targets: list[tuple[str, str]] | None = None) -> int:
        """Reset the given (provider, key) records, or every record. Returns how many."""
        states = self.all() if targets is None else [
            st for st in (self.load(p, k) for p, k in targets) if st is not None
        ]
        for st in states:
            st.reset()
        return len(states)
