from abc import ABC, abstractmethod
from datetime import datetime
from logging import Logger
from pathlib import Path
import re
from typing import List, Tuple
import os
import subprocess
from src.models import Song

class BaseDownloader(ABC):
    def __init__(self, output_dir: Path, logger: Logger):
        self.output_dir = output_dir
        self.logger = logger
        self.errors_dir = output_dir / ".errors"
        self.errors_dir.mkdir(parents=True, exist_ok=True)

    @abstractmethod
    def download(self, link: str) -> Tuple[int, Path, Path | None]:
        """Return (return_code, errors_file, playlist_dir)."""
        raise NotImplementedError


    @abstractmethod
    def cleanup(self, playlist_name: str) -> List[Song]:
        raise NotImplementedError
    
    @abstractmethod
    def fetch_metadata_image(self, link: str) -> str:
        """Fetch playlist name from metadata image URL."""
        raise NotImplementedError
    
    def _download(self, name: str, link: str, cmd: List[str], _errors_file: Path = None) -> Tuple[int, Path]:
        
        errors_file = self.errors_dir / f"errors-{name}-{datetime.now().strftime('%Y%m%d%H%M%S')}.txt" if not _errors_file else _errors_file

        self.logger.info(f"🎵 {name}: {link.split('?')[0]}")
        self.logger.info(f"📁 → {self.output_dir}")
        self.logger.debug(f"Command: {' '.join(cmd)}")
        
        env = os.environ.copy()
        try:
            # Start the subprocess and stream its combined stdout/stderr to the provided logger
            proc = subprocess.Popen(cmd, env=env, cwd=str(self.output_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            # Reader thread: logs each output line as it arrives
            def _reader(pipe, logger):
                try:
                    for raw in iter(pipe.readline, ''):
                        if raw is None:
                            break
                        line = raw.rstrip('\n')
                        if not line:
                            continue
                        # Filter out verbose DEBUG lines from subtools so only INFO-level messages show
                        # Many libraries print their own 'DEBUG' markers; skip those lines.
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
                proc.kill()
                reader.join(timeout=5)
                self.logger.warning(f"⏰ {name} timeout (1h)")
                return 1, errors_file

            reader.join()

            if proc.returncode == 0:
                self.logger.info(f"✅ {name} complete")
            else:
                self.logger.warning(f"{name} exit code: {proc.returncode}")
            return proc.returncode, errors_file
        except subprocess.TimeoutExpired:
            self.logger.warning(f"⏰ {name} timeout (1h)")
            return 1, errors_file
        except Exception as e:
            self.logger.error(f"💥 {name} error: {e}")
            return 1, errors_file
        
    def _get_padding(self, playlist_dir: Path) -> List[int]:
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

        # if not numbers:
        #     self.logger.info(f"ℹ️ No numbered files in: {playlist_name}")
        #     return [], 0
        
        return numbers, padding