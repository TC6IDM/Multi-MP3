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
                  on_progress: Callable[[str, Dict[str, Any]], None] | None = None,
                  stats_out: Dict[str, Any] | None = None) -> Tuple[int, Path]:
        """Run a downloader subprocess, parsing its output for progress.

        `stats_out`, when given, IS the live stats dict the reader thread
        updates, so a caller can inspect how far a run got and how many items
        failed — the return code alone can't say, since yt-dlp and scdl exit 0
        even when most of a playlist failed.
        """

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
        # SoundCloud (and any source already serving mp3) never emits an
        # ExtractAudio line, because there is nothing to convert — the file
        # lands straight from "[download] Destination: ....mp3". Treating that
        # as merely "in flight" meant those tracks never resolved at all.
        # Restricted to .mp3 on purpose: every downloader here targets mp3, so
        # .m4a/.webm/.opus are intermediates that will still be extracted, and
        # counting those would complete each YouTube track twice.
        re_yt_dest_final = re.compile(
            r'\[download\]\s+Destination:\s+(.+\.mp3)\s*$', re.IGNORECASE)
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
        # spotdl also fails without ever naming a Spotify URL, e.g.
        #   LookupError: No results found for song: SHAKING - CARBONE
        # Here the song title is the only handle we get.
        re_spotdl_notfound = re.compile(
            r'(?:LookupError:\s*)?No results found for song:\s*(.+?)\s*$'
        )
        # A per-item extractor error. SoundCloud emits one of these for every
        # remaining track once it rate-limits the IP, and yt-dlp still exits 0,
        # so without this the run looked successful while hundreds failed.
        re_item_error = re.compile(
            r'^ERROR:\s*(?:\[[^\]]+\]\s*)?(.+?)\s*$'
        )

        env = os.environ.copy()
        # Shared with the reader thread so the outcome can be reported even
        # when the tool exits 0 after failing most of its items.
        stats: Dict[str, Any] = stats_out if stats_out is not None else {}
        stats.update({"item": 0, "total": 0, "errors": 0, "last_error": ""})
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
                        # yt-dlp writes "[debug]" lowercase, which slipped past
                        # a case-sensitive check and flooded the log.
                        if 'DEBUG' in line or '[debug]' in line:
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
                                stats["item"] = int(m.group(1))
                                stats["total"] = int(m.group(2))
                                cb("track", {"total": int(m.group(2)), "current": int(m.group(1))})
                                continue
                            # Per-item failure: attribute it to whichever item
                            # is currently in flight, since these errors carry
                            # no title of their own.
                            m = re_item_error.search(line)
                            if m:
                                stats["errors"] += 1
                                stats["last_error"] = m.group(1)[:200]
                                cb("track_failed", {"index": stats["item"],
                                                    "error": m.group(1)[:200]})
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
                            # Already-final audio (SoundCloud http_mp3, etc.):
                            # no extraction step follows, so this IS completion.
                            m = re_yt_dest_final.search(line)
                            if m:
                                cb("track_complete", {"filename": m.group(1).strip()})
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
                            m = re_spotdl_notfound.search(line)
                            if m:
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

            rc = proc.returncode
            errs, total = stats["errors"], stats["total"]

            if errs:
                # yt-dlp/scdl exit 0 even when most items failed, which made a
                # rate-limited run look like a success.
                scope = f"{errs}/{total}" if total else str(errs)
                self.logger.warning(
                    f"⚠️ {name}: {scope} item(s) failed — last: {stats['last_error']}")
                if total and errs >= max(5, total * 0.5):
                    self.logger.error(
                        f"🚫 {name} lost more than half the playlist. SoundCloud "
                        f"rate-limits by IP; wait a while and re-run — already "
                        f"downloaded files are skipped.")
                    if cb:
                        cb("error", {"message": f"{scope} items failed (rate limited?)"})
                    rc = rc or 1
                elif rc == 0:
                    rc = 0   # a handful of bad tracks is not a failed run

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

    def search_and_download(self, title: str, artists: List[str],
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
        if target.exists():
            return True   # another attempt already recovered it

        cmd = [
            "yt-dlp",
            "--extract-audio", "--audio-format", "mp3",
            "--audio-quality", "1",
            "--embed-thumbnail", "--add-metadata",
            "--js-runtimes", "deno",
            "--no-playlist", "--no-warnings",
            "--ignore-errors", "--no-abort-on-error",
            "--output", str(out_dir / f"{stem}.%(ext)s"),
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
                cmd, cwd=str(self.output_dir), timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
        except subprocess.TimeoutExpired:
            say(f"⏰ Retry timed out: {query}", warn=True)
            return False
        except Exception as e:
            say(f"Retry failed to start: {e}", warn=True)
            return False

        # yt-dlp exits 0 even when a search yields nothing, so confirm the file
        if target.exists() and target.stat().st_size > 0:
            say(f"✅ Recovered: {stem}")
            return True

        tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
        say(f"❌ No YouTube match for {query}: {tail[0][:120]}", warn=True)
        return False

    @staticmethod
    def _parse_spotdl_errors(errors_file: Path) -> List[str]:
        """Extract failed Spotify track ids from spotdl's --save-errors file.

        The file holds a timestamp line followed by one entry per failure:
          https://open.spotify.com/track/<id> - AudioProviderError: ...
        """
        if not errors_file or not errors_file.is_file():
            return []
        pat = re.compile(r'open\.spotify\.com/track/([A-Za-z0-9]+)')
        ids: List[str] = []
        try:
            for line in errors_file.read_text(encoding="utf-8", errors="replace").splitlines():
                m = pat.search(line)
                if m and m.group(1) not in ids:
                    ids.append(m.group(1))
        except Exception:
            pass
        return ids

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