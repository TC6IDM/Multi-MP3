import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from src import tidy
from src.downloaders.base import BaseDownloader
from src.state import DONE, FAILED, PENDING, UNAVAILABLE, PlaylistState, StateStore
from src.utils import spotdl_command, spotdl_python

SPOTDL_RUNNER = Path(__file__).with_name("spotdl_runner.py")
SONGS_MARKER = "@@MULTIMP3_SONGS@@"

# spotdl's output template per kind of link. The leading number is what lets a
# file be matched back to its track: list position for playlists, the album's
# own track number for albums. Artist pages and single tracks have no number,
# so those are matched by name. Numbered files are renamed "(0001) Title.mp3"
# once the record has them (src/tidy.py); spotdl only ever writes new ones.
OUTPUT_TEMPLATES = {
    "playlist": "{list-name}/{list-position} {title} - {artists}.{output-ext}",
    "album": "{list-name}/{track-number} {title} - {artists}.{output-ext}",
    "artist": "{list-name}/{title} - {artists}.{output-ext}",
    "track": "{title}/{title} - {artists}.{output-ext}",
}
_KIND_RE = re.compile(r"/(playlist|album|artist|track)/")
_LEADING_NUMBER = re.compile(r"^\s*\(?\d+\)?\s+")


def link_kind(link: str) -> str:
    m = _KIND_RE.search(link)
    return m.group(1) if m else "playlist"


def spotdl_folder_name(name: str) -> str:
    """The folder name spotdl derives from {list-name} (its sanitize_string)."""
    out = "".join(c for c in name if c not in "/?\\*|<>")
    return out.replace('"', "'").replace(":", "-").strip()


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())


class SpotifyDownloader(BaseDownloader):
    """Spotify through spotdl, which is also the source of each playlist's track list.

    spotdl reads Spotify without the developer API by default, so no Spotify
    app credentials (and no Premium on the account that owns them) are needed.
    The track list comes from a handshake with spotdl_runner.py in the same
    run: spotdl lists the songs, the app answers with the ones it already has,
    and spotdl downloads only the rest.
    """

    def __init__(self, output_dir: Path, logger: logging.Logger,
                 client_id: str = "", client_secret: str = ""):
        super().__init__(output_dir, logger)
        self.client_id = client_id
        self.client_secret = client_secret
        self.threads = 6

    # ── Running spotdl ───────────────────────────────────────────────

    def download(self, link: str,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None,
                 on_songs: Callable[[list[dict[str, Any]]], list[str]] | None = None,
                 ) -> tuple[int, Path]:
        """Run spotdl for one link.

        With `on_songs`, spotdl runs through spotdl_runner.py: it hands over the
        playlist's songs, and `on_songs` returns the URLs to skip. Those that
        are downloaded overwrite any existing file — old copies have already
        been set aside, so nothing is lost if a download fails.
        """
        errors_file = self.errors_dir / f"errors-spotdl-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.txt"
        spotdl = spotdl_command()
        if not spotdl:
            self.logger.error("❌ spotdl not found. It runs from its own virtualenv: "
                              "`python -m venv .venv-spotdl` then "
                              "`.venv-spotdl\\Scripts\\pip install spotdl`, or set SPOTDL_BIN.")
            if on_progress:
                on_progress("error", {"message": "spotdl not installed"})
            return 1, errors_file

        python = spotdl_python() if on_songs else None
        # -I: run isolated. Otherwise Python puts this folder first on the
        # import path, and spotdl's own `import soundcloud` finds our
        # soundcloud.py instead of its library — it died on startup that way.
        # -X utf8: -I also ignores PYTHONIOENCODING, so on Windows the song
        # list came through in cp1252 and was read as UTF-8 — "Inéz" was
        # recorded as "In�z", which tagging would then have written into the file.
        head = [python, "-I", "-X", "utf8", str(SPOTDL_RUNNER)] if python else [spotdl]
        args = ["--save-errors", str(errors_file)]
        if self.client_id and self.client_secret:
            args += ["--client-id", self.client_id, "--client-secret", self.client_secret]
        args += [
            "--output", OUTPUT_TEMPLATES[link_kind(link)],
            # Rich's TUI wraps long lines inside a bordered box, which split
            # `Downloaded "Some Title"` across two lines and broke progress
            # parsing entirely. --simple-tui emits plain, unwrapped lines.
            "--simple-tui",
            "--log-level", "INFO",
            "--threads", str(max(1, self.threads)),
            "--overwrite", "force" if python else "skip",
            # spotdl drives its own bundled yt-dlp, which needs a JS runtime for
            # YouTube's challenge — without it every track fails with HTTP 403.
            # (deno's directory is put on PATH by _download.)
            "--yt-dlp-args", "--js-runtimes deno",
            "download", link,
        ]
        if on_songs and not python:
            self.logger.warning("⚠️ spotdl isn't in a virtualenv of its own, so this run can't keep "
                                "a per-track record (set SPOTDL_PYTHON to its interpreter).")
        return self._download("SpotDL", link, head + args, _errors_file=errors_file,
                              on_progress=on_progress, on_songs=on_songs if python else None)

    # ── Track list ───────────────────────────────────────────────────

    @staticmethod
    def tracks_from_songs(songs: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
        """spotdl's song list → the record's track entries."""
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for i, s in enumerate(songs, 1):
            pos = int(s.get("list_position") or i)
            key = str(s.get("song_id") or s.get("url") or f"pos-{pos}")
            if key in seen:                       # the same song twice in a playlist
                key = f"{key}#{pos}"
            seen.add(key)
            file_index = (int(s["track_number"]) if kind == "album" and s.get("track_number")
                          else pos if kind == "playlist" else None)
            out.append({
                "key": key, "index": pos, "num": pos, "id": str(s.get("song_id") or ""),
                "url": s.get("url"), "title": s.get("name") or "",
                "artists": [a for a in (s.get("artists") or []) if a],
                "duration_ms": int(float(s.get("duration") or 0) * 1000),
                "file_index": file_index,
            })
        return out

    def folder_for(self, songs: list[dict[str, Any]], kind: str) -> str:
        """The folder spotdl writes this link's files into (an existing one if found)."""
        if not songs:
            return ""
        first = songs[0]
        name = (first.get("name") if kind == "track" else first.get("list_name")) or ""
        return self.find_folder(name)

    @staticmethod
    def list_name(songs: list[dict[str, Any]], kind: str, fallback: str) -> str:
        if not songs:
            return fallback
        first = songs[0]
        return str((first.get("name") if kind == "track" else first.get("list_name")) or fallback)

    def find_folder(self, name: str) -> str:
        """spotdl's folder for `name`, preferring one that already exists
        (case-insensitively — Windows doesn't care, and older runs used a
        slightly different sanitiser)."""
        candidates = [spotdl_folder_name(name),
                      "".join(c for c in name if c not in '<>:"/\\|?*').strip(),
                      name.strip()]
        for c in candidates:
            if c and (self.output_dir / c).is_dir():
                return c
        try:
            existing = {p.name.lower(): p.name for p in self.output_dir.iterdir() if p.is_dir()}
        except OSError:
            existing = {}
        for c in candidates:
            if c and c.lower() in existing:
                return existing[c.lower()]
        return candidates[0]

    @staticmethod
    def _name_key(track: dict[str, Any]) -> str:
        return _norm(f"{track.get('title', '')} - {', '.join(track.get('artists') or [])}")

    def match_files(self, folder: str, tracks: list[dict[str, Any]]) -> dict[str, Path]:
        """Which file on disk belongs to which track: by the number in the
        filename (when the name fits the title) where there is one, by
        "title - artists" where there isn't."""
        if not folder:
            return {}
        directory = self.output_dir / folder
        if any(t.get("file_index") is not None for t in tracks):
            return tidy.match_files(directory, tracks, number_key="file_index")
        by_name: dict[str, Path] = {}
        try:
            for p in directory.iterdir():
                if p.suffix.lower() == ".mp3" and p.is_file():
                    by_name.setdefault(_norm(_LEADING_NUMBER.sub("", p.stem)), p)
        except OSError:
            return {}
        return {t["key"]: by_name[k] for t in tracks if (k := self._name_key(t)) in by_name}

    # ── Progress record ──────────────────────────────────────────────

    def prepare_state(self, store: StateStore, link: str, name: str, folder: str,
                      tracks: list[dict[str, Any]]) -> PlaylistState:
        """Sync the playlist's record with spotdl's track list and the disk."""
        st = store.open("spotify", link)
        first_sight = st.is_new
        st.update_meta(name=name, folder=folder, url=link)
        st.sync_tracks(tracks)
        if first_sight:
            # Seen for the first time: whatever an earlier version already
            # downloaded counts as done, so nothing gets fetched again.
            for key, path in self.match_files(folder, tracks).items():
                st.mark(key, DONE, file=path, save=False)
        st.heal()
        st.save()
        return st

    @staticmethod
    def skip_urls(st: PlaylistState) -> list[str]:
        """The tracks spotdl shouldn't touch: done, or gone from YouTube."""
        return [t["url"] for t in st.ordered()
                if t.get("status") in (DONE, UNAVAILABLE) and t.get("url")]

    # ── Old copies of tracks being re-downloaded ─────────────────────

    def _set_aside_dir(self, st: PlaylistState) -> Path:
        return self.output_dir / ".partial" / "spotify-set-aside" / st.key

    def set_aside_stale(self, st: PlaylistState, folder: str, tracks: list[dict[str, Any]]) -> int:
        """Move old copies of tracks about to be re-downloaded out of the way.

        spotdl's ffmpeg writes straight over an existing file, and deletes the
        output if conversion fails — so a reset playlist whose re-download was
        cancelled or failed could lose the copy it had. Moving the old files
        aside (an instant rename on the same disk) and moving back whatever
        didn't get a complete replacement keeps them safe.
        """
        self.restore_stale(st, folder, tracks)   # anything a crashed run left aside
        aside = self._set_aside_dir(st)
        files = self.match_files(folder, tracks)
        manifest: dict[str, str] = {}
        for t in st.ordered():
            if t.get("status") in (DONE, UNAVAILABLE):
                continue
            src = files.get(t["key"])
            if src is None:
                continue
            try:
                aside.mkdir(parents=True, exist_ok=True)
                src.replace(aside / src.name)
                manifest[src.name] = t["key"]
            except OSError as e:
                self.logger.debug(f"Couldn't set aside {src.name}: {e}")
        if manifest:
            (aside / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return len(manifest)

    def restore_stale(self, st: PlaylistState, folder: str,
                      tracks: list[dict[str, Any]] | None = None) -> int:
        """Put set-aside copies back wherever no new file took their place."""
        aside = self._set_aside_dir(st)
        if not aside.is_dir():
            return 0
        try:
            manifest = json.loads((aside / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        current = self.match_files(folder, tracks) if tracks else {}
        dest = self.output_dir / folder
        restored = 0
        for old in list(aside.iterdir()):
            if old.name == "manifest.json":
                continue
            key = manifest.get(old.name)
            replaced = (key in current) if key else (dest / old.name).exists()
            if replaced or (dest / old.name).exists():
                old.unlink(missing_ok=True)      # a fresh download took its place
                continue
            try:
                dest.mkdir(parents=True, exist_ok=True)
                old.replace(dest / old.name)
                restored += 1
            except OSError as e:
                self.logger.warning(f"Couldn't restore {old.name}: {e}")
        (aside / "manifest.json").unlink(missing_ok=True)
        try:
            aside.rmdir()
        except OSError:
            pass
        return restored

    @staticmethod
    def _looks_truncated(path: Path, duration_ms: int) -> bool:
        """True for an MP3 cut off mid-write (spotdl killed during conversion)."""
        if not duration_ms:
            return False
        try:
            from mutagen.mp3 import MP3
            info: Any = MP3(path).info
            length = float(info.length)
        except Exception:
            return True   # unreadable — not something to call done
        # Generous: YouTube uploads often differ from Spotify by seconds.
        return length < 0.85 * duration_ms / 1000 - 3

    def reconcile(self, st: PlaylistState, folder: str, tracks: list[dict[str, Any]],
                  run_started: float, errors_file: Path | None, completed: bool,
                  check_truncation: bool = False) -> None:
        """Record what this run actually produced.

        The files on disk are the truth: a file written during this run means
        done; anything older is a stale copy from before a reset. With
        `check_truncation` (spotdl didn't exit cleanly) a fresh file much
        shorter than the song is one it was killed in the middle of writing,
        so it's removed and the track stays to do.
        """
        files = self.match_files(folder, tracks)
        errors = self._parse_spotdl_error_lines(errors_file) if errors_file else {}
        for t in st.ordered():
            if t.get("status") in (DONE, UNAVAILABLE):
                continue
            path = files.get(t["key"])
            fresh = False
            if path is not None:
                try:
                    fresh = path.stat().st_mtime >= run_started - 5
                except OSError:
                    fresh = False
            if fresh and path is not None and check_truncation \
                    and self._looks_truncated(path, int(t.get("duration_ms") or 0)):
                self.logger.warning(f"⚠️ {path.name} was cut off mid-write — removing it")
                path.unlink(missing_ok=True)
                fresh = False
            if fresh:
                st.mark(t["key"], DONE, file=path, save=False)
            elif t.get("id") in errors:
                st.mark(t["key"], FAILED, error=errors[t["id"]] or "spotdl couldn't download this track",
                        save=False)
            elif completed:
                st.mark(t["key"], FAILED, error="Not downloaded", save=False)
            else:
                st.mark(t["key"], PENDING, save=False)   # cancelled before reaching it
        st.save()

    def save_cover(self, url: str | None, folder: str) -> None:
        """The first song's artwork as the playlist's cover in the Library."""
        if not url or not folder:
            return
        target = self.output_dir / ".icons" / f"{Path(folder).name}.jpg"
        if target.exists():
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=15) as resp:
                target.write_bytes(resp.read())
        except Exception as e:
            self.logger.debug(f"Cover fetch failed: {e}")
