from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger
from pathlib import Path
import re
from typing import List, Tuple
import json
import os
import subprocess
import psutil
from src.models import Song, Playlist

class BaseDownloader(ABC):
    def __init__(self, output_dir: Path, logger: Logger):
        self.output_dir = output_dir
        self.logger = logger
        self.errors_dir = output_dir / ".errors"
        self.errors_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def download(self, link: str) -> Tuple[int, Path]:
        """Return (return_code, errors_file)."""
        raise NotImplementedError


    @abstractmethod
    def cleanup(self, playlist_name: str) -> List[Song]:
        raise NotImplementedError
    
    @abstractmethod
    def fetch_metadata_image(self, link: str) -> str:
        """Fetch playlist name from metadata image URL."""
        raise NotImplementedError
    
    def _download(self, name: str, link: str, cmd: List[str], _errors_file: Path | None = None) -> Tuple[int, Path]:

        errors_file = self.errors_dir / f"errors-{name}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt" if not _errors_file else _errors_file

        self.logger.info(f"🎵 {name}: {link.split('?')[0]}")
        self.logger.info(f"📁 → {self.output_dir}")
        self.logger.debug(f"Command: {' '.join(cmd)}")

        env = os.environ.copy()
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(self.output_dir),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            def _reader(pipe, logger):
                try:
                    for raw in iter(pipe.readline, ''):
                        if raw is None:
                            break
                        line = raw.rstrip('\n')
                        if not line:
                            continue
                        if 'DEBUG' in line:
                            continue
                        logger.info(line)
                except Exception:
                    pass

            from threading import Thread
            reader = Thread(target=_reader, args=(proc.stdout, self.logger), daemon=True)
            reader.start()

            try:
                proc.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                self._kill_process_tree(proc)
                reader.join(timeout=5)
                self.logger.warning(f"⏰ {name} timeout (1h)")
                return 1, errors_file

            reader.join()

            if proc.returncode == 0:
                self.logger.info(f"✅ {name} complete")
            else:
                self.logger.warning(f"{name} exit code: {proc.returncode}")
            return proc.returncode, errors_file
        except Exception as e:
            self.logger.error(f"💥 {name} error: {e}")
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

    def _cleanup_all_playlists(self) -> List[Song]:
        """Iterate all playlist dirs and run per-playlist cleanup. Used by YouTube and SoundCloud."""
        metadata_root = self.output_dir / ".metadata"
        metadata_root.mkdir(exist_ok=True)
        all_missing: List[Song] = []

        for playlist_dir in self.output_dir.iterdir():
            if not playlist_dir.is_dir() or playlist_dir.name.startswith('.'):
                continue
            missing = self._cleanup_playlist(playlist_dir, metadata_root)
            all_missing.extend(missing)

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