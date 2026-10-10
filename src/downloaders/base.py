import json
import logging
import re
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from logging import Logger
from pathlib import Path
from typing import Any

import psutil

from src.utils import deno_path, subprocess_env

# High-frequency subprocess output that must never reach the SSE stream.
# Progress ticks arrive many times per second per download; forwarding them
# floods the browser and freezes the log pane.
_NOISE = re.compile(
    r'\[download\]\s+\d+\.?\d*%'          # yt-dlp percentage ticks
    r'|ETA\s+\d'                           # ETA updates
    r'|\d+\.?\d*(?:Ki|Mi|Gi)?B/s'          # transfer-rate updates
    r'|frame=\s*\d+'                       # ffmpeg progress
    r'|size=\s*\d+'                        # ffmpeg size updates
    r'|\r'                                 # carriage-return redraws
)

# spotdl_runner.py prints the playlist's song list on one line behind this.
SONGS_MARKER = "@@MULTIMP3_SONGS@@"

# A subprocess that prints nothing for this long is wedged. This replaces a flat
# one-hour cap on the whole run, which a large Spotify playlist could exceed
# while working perfectly.
IDLE_TIMEOUT = 15 * 60


class BaseDownloader(ABC):
    """Shared plumbing for downloaders that drive an external tool (spotdl)."""

    def __init__(self, output_dir: Path, logger: Logger):
        self.output_dir = output_dir
        self.logger = logger
        self.errors_dir = output_dir / ".errors"
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        # Replaced by the coordinator's event so Cancel reaches running tools.
        self.cancel = threading.Event()

    @abstractmethod
    def download(self, link: str,
                 on_progress: Callable[[str, dict[str, Any]], None] | None = None
                 ) -> tuple[int, Path]:
        """Return (return_code, errors_file).

        `on_progress` is passed per call rather than stored on the instance so
        that one downloader can safely serve concurrent links.
        """
        raise NotImplementedError

    def _download(self, name: str, link: str, cmd: list[str],
                  _errors_file: Path | None = None,
                  on_progress: Callable[[str, dict[str, Any]], None] | None = None,
                  stats_out: dict[str, Any] | None = None,
                  on_songs: Callable[[list[dict[str, Any]]], list[str]] | None = None,
                  ) -> tuple[int, Path]:
        """Run a downloader subprocess, parsing its output for progress.

        `stats_out`, when given, IS the live stats dict the reader thread
        updates, so a caller can see how far a run got.

        `on_songs` answers spotdl_runner.py's handshake: it receives the song
        list the runner prints and returns the URLs to skip, which are written
        back on the child's stdin.
        """
        errors_file = _errors_file or (
            self.errors_dir / f"errors-{name}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.txt")

        self.logger.info(f"🎵 {name}: {link.split('?')[0]}")
        self.logger.info(f"📁 → {self.output_dir}")

        # Per-call callback keeps this instance safe for concurrent links
        cb = on_progress

        # spotdl titles can still arrive without a closing quote if any TUI
        # wrapping occurs, so the closing quote is optional and a trailing
        # "  module.py:123" suffix (Rich's log location column) is trimmed.
        re_spotdl_done = re.compile(r'Downloaded\s+"([^"]*?)(?:"|\s{2,}\S+\.py:\d+|$)')
        re_spotdl_skip = re.compile(
            r'"([^"]*?)"\s+(?:is\s+)?already\s+(?:downloaded|exists)'
            r'|Skipping\s+(.+?)\s+\(file already exists\)'
        )
        re_spotdl_search = re.compile(r'Searching for\s+"([^"]*?)(?:"|\s{2,}\S+\.py:\d+|$)')
        # --simple-tui emits per-song status lines ("Artist - Title: Downloading")
        # and a running tally ("7/23 complete"). Deliberately excludes ": Done",
        # because spotdl also prints `Downloaded "..."` for the same track and
        # counting both would advance progress twice.
        re_spotdl_active = re.compile(
            r'^\s*(.+?):\s+(?:Searching for song|Getting audio meta|Downloading'
            r'|Embedding metadata|Converting)\s*$'
        )
        re_spotdl_tally = re.compile(r'^\s*(\d+)/(\d+)\s+complete\s*$')
        # spotdl's end-of-run failure summary identifies the track by its
        # Spotify URL, which is the only reliable way to attribute a failure:
        #   https://open.spotify.com/track/<id> - AudioProviderError: ...
        re_spotdl_fail = re.compile(
            r'https?://open\.spotify\.com/track/([A-Za-z0-9]+)\S*\s*-\s*(.+)'
        )
        # spotdl also fails without ever naming a Spotify URL, e.g.
        #   LookupError: No results found for song: SHAKING - CARBONE
        # Here the song title is the only handle we get.
        re_spotdl_notfound = re.compile(
            r'(?:LookupError:\s*)?No results found for song:\s*(.+?)\s*$'
        )

        stats: dict[str, Any] = stats_out if stats_out is not None else {}
        stats.update({"errors": 0, "last_error": "", "last_output": time.monotonic()})
        try:
            # deno's directory goes on PATH: spotdl's bundled yt-dlp looks it
            # up there for YouTube's JS challenge.
            proc = subprocess.Popen(cmd, env=subprocess_env(), cwd=str(self.output_dir),
                                    stdin=subprocess.PIPE if on_songs else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace")

            def _answer_songs(line: str) -> None:
                skip: list[str] = []
                try:
                    songs = json.loads(line[len(SONGS_MARKER):].strip() or "[]")
                    if on_songs is not None:
                        skip = list(on_songs(songs))
                except Exception as e:
                    # Answer anyway: an unanswered runner would wait forever.
                    self.logger.error(f"💥 Couldn't prepare the track list: {e}")
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(json.dumps({"skip": skip}) + "\n")
                        proc.stdin.flush()
                        proc.stdin.close()
                    except OSError:
                        pass

            def _reader(pipe: Any, logger: Logger) -> None:
                # Hoist attribute lookups out of the loop — this runs once per
                # subprocess line, tens of thousands of times per playlist.
                debug_on = logger.isEnabledFor(logging.DEBUG)
                log_debug = logger.debug
                noise_search = _NOISE.search
                try:
                    for raw in iter(pipe.readline, ''):
                        stats["last_output"] = time.monotonic()
                        line = raw.rstrip('\n')
                        if not line:
                            continue
                        # Before every filter: a song list is one long line
                        # that could contain anything, including "ETA 3".
                        if line.startswith(SONGS_MARKER):
                            _answer_songs(line)
                            continue
                        if 'DEBUG' in line or '[debug]' in line:
                            continue
                        if noise_search(line):
                            if debug_on:
                                log_debug(line)
                            continue

                        # Subprocess output goes to file/console at DEBUG so the
                        # INFO-level SSE handler doesn't duplicate it — the web UI
                        # receives it via cb("log") routed to the owning playlist.
                        if debug_on:
                            log_debug(line)
                        if not cb:
                            continue
                        cb("log", {"line": line[:400]})

                        # Checked before the completion patterns: a failure
                        # line also contains a track URL and would otherwise be
                        # misread as progress.
                        m = re_spotdl_fail.search(line)
                        if m:
                            stats["errors"] += 1
                            stats["last_error"] = m.group(2).strip()[:200]
                            cb("track_failed", {"track_id": m.group(1),
                                                "error": m.group(2).strip()[:200]})
                            continue
                        m = re_spotdl_notfound.search(line)
                        if m:
                            stats["errors"] += 1
                            cb("track_failed", {"title": m.group(1).strip(),
                                                "error": "No match found on YouTube"})
                            continue
                        m = re_spotdl_done.search(line)
                        if m:
                            cb("track_complete", {"title": m.group(1).strip()})
                            continue
                        m = re_spotdl_skip.search(line)
                        if m:
                            title = (m.group(1) or m.group(2) or "").strip()
                            cb("track_complete", {"title": title})
                            continue
                        m = re_spotdl_search.search(line)
                        if m:
                            cb("searching", {"query": m.group(1).strip()})
                            continue
                        m = re_spotdl_tally.search(line)
                        if m:
                            cb("tally", {"done": int(m.group(1)), "total": int(m.group(2))})
                            continue
                        m = re_spotdl_active.search(line)
                        if m:
                            cb("searching", {"query": m.group(1).strip()})
                            continue
                except Exception:
                    pass

            reader = threading.Thread(target=_reader, args=(proc.stdout, self.logger), daemon=True,
                                      name=f"reader-{name}")
            reader.start()

            # Poll rather than block, so Cancel and a wedged tool are noticed.
            while True:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.cancel.is_set():
                    self._kill_process_tree(proc)
                    reader.join(timeout=5)
                    self.logger.info(f"⏹️ {name} stopped (cancelled)")
                    return 130, errors_file
                if time.monotonic() - stats["last_output"] > IDLE_TIMEOUT:
                    self._kill_process_tree(proc)
                    reader.join(timeout=5)
                    self.logger.warning(f"⏰ {name} printed nothing for {IDLE_TIMEOUT // 60} min — stopped")
                    if cb:
                        cb("error", {"message": "stalled — no output for 15 min"})
                    return 1, errors_file

            reader.join()
            rc = proc.returncode
            if stats["errors"]:
                self.logger.warning(f"⚠️ {name}: {stats['errors']} track(s) failed — last: {stats['last_error']}")
            if rc == 0:
                self.logger.info(f"✅ {name} complete")
            else:
                self.logger.warning(f"{name} exit code: {rc}")
            return rc, errors_file
        except Exception as e:
            self.logger.error(f"💥 {name} error: {e}")
            if cb:
                cb("error", {"message": str(e)})
            return 1, errors_file

    # Characters Windows forbids in filenames; spotdl strips these too, so
    # matching its behaviour keeps recovered files alongside the rest.
    _BAD_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

    def search_and_download(self, title: str, artists: list[str],
                            playlist_dir: str, position: int | None = None,
                            padding: int = 2, timeout: int = 600,
                            on_log: Callable[[str], None] | None = None) -> bool:
        """Last-resort recovery for a single track.

        spotdl picks a YouTube match itself and gives up if that one URL fails,
        so a track can be unavailable purely because of the candidate it chose.
        Searching YouTube directly often finds a working alternative.

        Returns True if a file was produced.
        """
        artist_str = ", ".join(a for a in artists if a)
        query = f"{artist_str} {title}".strip() if artist_str else title.strip()
        if not query:
            return False

        stem = f"{title} - {artist_str}" if artist_str else title
        if position:
            stem = f"{position:0{padding}d} {stem}"
        stem = self._BAD_FILENAME.sub("_", stem).strip()

        out_dir = self.output_dir / playlist_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"{stem}.mp3"
        # Downloaded under a temporary name and moved into place only when
        # complete, so neither a stale copy nor a half-written file can pass
        # for a recovered track.
        temp_stem = f".{stem}.recovering"
        temp = out_dir / f"{temp_stem}.mp3"

        deno = deno_path()
        cmd = [
            # This interpreter's yt-dlp, not whatever `yt-dlp` is on PATH — a
            # venv's Scripts directory is only on PATH when activated.
            sys.executable, "-m", "yt_dlp",
            "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "1",
            "--embed-thumbnail", "--add-metadata",
            "--js-runtimes", f"deno:{deno}" if deno else "deno",
            "--no-playlist", "--no-warnings",
            "--ignore-errors", "--no-abort-on-error",
            "--force-overwrites",
            "--output", str(out_dir / f"{temp_stem}.%(ext)s"),
            f"ytsearch1:{query}",
        ]

        # Retry chatter belongs to the playlist that owns the track, not the
        # global log, so it goes through on_log when one is supplied.
        def say(msg: str, warn: bool = False) -> None:
            if on_log:
                on_log(msg)
            else:
                (self.logger.warning if warn else self.logger.info)(msg)

        say(f"🔁 Retrying via YouTube search: {query}")
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.output_dir), timeout=timeout, env=subprocess_env(),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            say(f"⏰ Retry timed out: {query}", warn=True)
            self._remove_temp(out_dir, temp_stem)
            return False
        except Exception as e:
            say(f"Retry failed to start: {e}", warn=True)
            return False

        # yt-dlp exits 0 even when a search yields nothing, so confirm the file
        if temp.exists() and temp.stat().st_size > 0:
            try:
                temp.replace(target)
            except OSError as e:
                say(f"❌ Couldn't move the recovered file into place: {e}", warn=True)
                self._remove_temp(out_dir, temp_stem)
                return False
            self._remove_temp(out_dir, temp_stem)
            say(f"✅ Recovered: {stem}")
            return True

        self._remove_temp(out_dir, temp_stem)
        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        say(f"❌ No YouTube match for {query}: {tail[0][:120]}", warn=True)
        return False

    @staticmethod
    def _remove_temp(folder: Path, temp_stem: str) -> None:
        """Delete a recovery attempt's leftovers (.part, thumbnail, …)."""
        try:
            for p in folder.iterdir():
                if p.name.startswith(temp_stem + "."):
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _parse_spotdl_errors(errors_file: Path) -> list[str]:
        """Extract failed Spotify track ids from spotdl's --save-errors file.

        The file holds a timestamp line followed by one entry per failure:
          https://open.spotify.com/track/<id> - AudioProviderError: ...
        """
        return list(BaseDownloader._parse_spotdl_error_lines(errors_file))

    @staticmethod
    def _parse_spotdl_error_lines(errors_file: Path) -> dict[str, str]:
        """Failed Spotify track id → spotdl's error text, in file order."""
        if not errors_file or not errors_file.is_file():
            return {}
        pat = re.compile(r'open\.spotify\.com/track/([A-Za-z0-9]+)\S*\s*-?\s*(.*)')
        found: dict[str, str] = {}
        try:
            for line in errors_file.read_text(encoding="utf-8", errors="replace").splitlines():
                m = pat.search(line)
                if m and m.group(1) not in found:
                    found[m.group(1)] = m.group(2).strip()[:300]
        except Exception:
            pass
        return found

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen) -> None:
        """Kill a process and all its children (e.g. ffmpeg grandchildren)."""
        try:
            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            _, alive = psutil.wait_procs(children, timeout=5)
            for child in alive:
                child.kill()
            parent.terminate()
            parent.wait(timeout=5)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            try:
                proc.kill()
            except Exception:
                pass
