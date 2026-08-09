from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import logging
from logging import Logger
from pathlib import Path
import re
from typing import Callable, List, Tuple, Dict, Any
import json
import os
import subprocess
import threading
import psutil
from src.models import Song, Playlist

# High-frequency subprocess output that must never reach the SSE stream.
# yt-dlp/scdl emit percentage + ETA lines many times per second per download;
# forwarding them floods the browser and freezes the log pane.
_NOISE = re.compile(
    r'\[download\]\s+\d+\.?\d*%'          # yt-dlp percentage ticks
    r'|ETA\s+\d'                           # ETA updates
    r'|\d+\.?\d*(?:Ki|Mi|Gi)?B/s'          # transfer-rate updates
    r'|frame=\s*\d+'                       # ffmpeg progress
    r'|size=\s*\d+'                        # ffmpeg size updates
    r'|\r'                                 # carriage-return redraws
)


class BaseDownloader(ABC):
    def __init__(self, output_dir: Path, logger: Logger):
        self.output_dir = output_dir
        self.logger = logger
        self.errors_dir = output_dir / ".errors"
        self.errors_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def download(self, link: str,
                 on_progress: Callable[[str, Dict[str, Any]], None] | None = None
                 ) -> Tuple[int, Path]:
        """Return (return_code, errors_file).

        `on_progress` is passed per call rather than stored on the instance so
        that one downloader can safely serve concurrent links.
        """
        raise NotImplementedError


    @abstractmethod
    def cleanup(self, playlist_name: str) -> List[Song]:
        raise NotImplementedError
    
    @abstractmethod
    def fetch_metadata_image(self, link: str) -> str:
        """Fetch playlist name from metadata image URL."""
        raise NotImplementedError
    
    def _download(self, name: str, link: str, cmd: List[str],
                  _errors_file: Path | None = None,
                  on_progress: Callable[[str, Dict[str, Any]], None] | None = None) -> Tuple[int, Path]:

        errors_file = self.errors_dir / f"errors-{name}-{datetime.now().strftime('%Y%m%d%H%M%S%f')}.txt" if not _errors_file else _errors_file

        self.logger.info(f"🎵 {name}: {link.split('?')[0]}")
        self.logger.info(f"📁 → {self.output_dir}")

        # Per-call callback keeps this instance safe for concurrent links
        cb = on_progress

        # Track-level progress patterns.
        # yt-dlp prints "Destination:" TWICE per track — once for the video
        # container ([download] ... .webm) and again after audio extraction
        # ([ExtractAudio] ... .mp3). Counting both double-counted every track
        # (progress read 41/40) and pushed titles out of alignment, so only the
        # final audio destination is treated as completion.
        re_yt_item = re.compile(r'\[download\]\s+Downloading item (\d+) of (\d+)')
        re_yt_done = re.compile(
            r'\[ExtractAudio\]\s+Destination:\s+(.+)'
            r'|\[download\]\s+(.+?)\s+has already been downloaded'
        )
        re_yt_dest_intermediate = re.compile(r'\[download\]\s+Destination:\s+(.+)')
        re_scdl_track = re.compile(r'\[(\d+)/(\d+)\].*Downloading')
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

        env = os.environ.copy()
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(self.output_dir),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            def _reader(pipe, logger):
                # Hoist attribute lookups out of the loop — this runs once per
                # subprocess line, tens of thousands of times per playlist.
                debug_on = logger.isEnabledFor(logging.DEBUG)
                log_debug = logger.debug
                noise_search = _NOISE.search
                try:
                    for raw in iter(pipe.readline, ''):
                        if raw is None:
                            break
                        line = raw.rstrip('\n')
                        if not line:
                            continue
                        if 'DEBUG' in line:
                            continue

                        # Drop high-frequency progress noise — yt-dlp/scdl emit
                        # percentage lines many times per second per download.
                        # These would flood the SSE stream and the browser DOM.
                        if noise_search(line):
                            if debug_on:
                                log_debug(line)
                            continue

                        # Subprocess output goes to file/console at DEBUG so the
                        # INFO-level SSE handler doesn't duplicate it — the web UI
                        # receives it via cb("log") routed to the owning playlist.
                        if debug_on:
                            log_debug(line)
                        if cb:
                            cb("log", {"line": line[:400]})

                        # Parse progress for web UI
                        if cb and line:
                            m = re_yt_item.search(line)
                            if m:
                                cb("track", {"total": int(m.group(2)), "current": int(m.group(1))})
                                continue
                            m = re_scdl_track.search(line)
                            if m:
                                cb("track", {"total": int(m.group(2)), "current": int(m.group(1))})
                                continue
                            m = re_yt_done.search(line)
                            if m:
                                filename = m.group(1) or m.group(2) or ""
                                cb("track_complete", {"filename": filename.strip()})
                                continue
                            # Intermediate container destination — names the
                            # track now in flight but is NOT a completion.
                            m = re_yt_dest_intermediate.search(line)
                            if m:
                                cb("track_active", {"filename": m.group(1).strip()})
                                continue
                            # Checked before the completion patterns: a failure
                            # line also contains a track URL and would
                            # otherwise be misread as progress.
                            m = re_spotdl_fail.search(line)
                            if m:
                                cb("track_failed", {"track_id": m.group(1),
                                                    "error": m.group(2).strip()[:200]})
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
                                cb("tally", {"done": int(m.group(1)),
                                             "total": int(m.group(2))})
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

            try:
                proc.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                reader.join(timeout=5)
                self.logger.warning(f"⏰ {name} timeout (1h)")
                if cb:
                    cb("error", {"message": "timeout (1h)"})
                return 1, errors_file

            reader.join()

            if proc.returncode == 0:
                self.logger.info(f"✅ {name} complete")
            else:
                self.logger.warning(f"{name} exit code: {proc.returncode}")
            return proc.returncode, errors_file
        except Exception as e:
            self.logger.error(f"💥 {name} error: {e}")
            if cb:
                cb("error", {"message": str(e)})
            return 1, errors_file

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
        
    def _get_padding(self, playlist_dir: Path) -> Tuple[List[int], int]:
        """Get zero-padding length for track numbers."""
        numbers = []
        padding = 0
        for p in playlist_dir.iterdir():
            if p.is_file() and p.suffix.lower() == '.mp3':
                match = re.match(r'^\s*(\d+)', p.stem)
                if match:
                    num_str = match.group(1)
                    numbers.append(int(num_str))
                    padding = max(padding, len(num_str))
        numbers.sort()

        return numbers, padding

    def _cleanup_all_playlists(self, max_workers: int = 4) -> List[Song]:
        """Iterate all playlist dirs and run per-playlist cleanup in parallel."""
        metadata_root = self.output_dir / ".metadata"
        metadata_root.mkdir(exist_ok=True)

        playlist_dirs = [d for d in self.output_dir.iterdir()
                         if d.is_dir() and not d.name.startswith('.')]
        if not playlist_dirs:
            return []

        all_missing: List[Song] = []
        with ThreadPoolExecutor(max_workers=min(max_workers, len(playlist_dirs))) as executor:
            futures = {
                executor.submit(self._cleanup_playlist, d, metadata_root): d.name
                for d in playlist_dirs
            }
            for future in as_completed(futures):
                try:
                    all_missing.extend(future.result())
                except Exception as e:
                    self.logger.error(f"💥 Cleanup failed for {futures[future]}: {e}")

        self.logger.info(f"🧹 Done! {len(all_missing)} total missing")
        return all_missing

    def _cleanup_playlist(self, playlist_dir: Path, metadata_root: Path) -> List[Song]:
        """Shared cleanup: aggregate info.json → .metadata, scan for missing MP3s."""
        playlist_name = playlist_dir.name
        info_files = list(playlist_dir.glob("*.info.json"))
        if not info_files:
            return []

        playlist_json_path = metadata_root / f"{playlist_name}.json"
        first_info = info_files[0]
        first_info.replace(playlist_json_path)

        try:
            with playlist_json_path.open("r") as f:
                playlist_data = json.load(f)
        except Exception:
            playlist_data = {}

        expected_count = playlist_data.get("playlist_count", len(info_files))
        self.logger.info(f"📊 {playlist_name}: {expected_count} expected")

        numbers, padding = self._get_padding(playlist_dir)
        missing_numbers = [n for n in range(1, expected_count + 1) if n not in numbers]

        missing_songs: List[Song] = []
        for num in missing_numbers:
            num_str = f"{num:0{padding}d}"
            missing_songs.append(Song(
                song_url="", playlist_url=playlist_data.get("webpage_url", ""),
                error=f"Missing {num_str}",
                playlist=Playlist(
                    playlist_url=playlist_data.get("webpage_url", ""),
                    name=playlist_name, length=expected_count
                ),
                list_position=num_str
            ))

        if missing_songs:
            self.logger.info(f"⚠️ {len(missing_songs)} missing in {playlist_name}:")
            for song in missing_songs:
                self.logger.info(f"  🚫 {song.error}")
        else:
            self.logger.info(f"✅ All {expected_count} present: {playlist_name}")

        # Aggregate remaining info.json → songs[]
        songs = []
        for info_file in info_files[1:]:
            try:
                with info_file.open("r") as f:
                    track_data = json.load(f)
                songs.append(track_data)
            except Exception:
                pass
        playlist_data["songs"] = songs

        with playlist_json_path.open("w", encoding="utf-8") as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)

        # Move description file
        desc_file = playlist_dir / f"{playlist_name}.description"
        if desc_file.exists():
            desc_file.replace(metadata_root / f"{playlist_name}.txt")

        # Delete leftover info.json files
        for info_file in info_files[1:]:
            info_file.unlink(missing_ok=True)

        self.logger.info(f"✅ {playlist_name}.json: {len(songs)} songs")
        return missing_songs