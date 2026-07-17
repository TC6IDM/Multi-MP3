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
        # Redirect subprocess output to the project's spotdl.log so the web UI can read progress
        log_path = self.output_dir / "spotdl.log"
        try:
            with open(log_path, "a", encoding="utf-8", errors="ignore") as lf:
                proc = subprocess.run(cmd, env=env, cwd=str(self.output_dir), 
                                      stdout=lf, stderr=lf, text=True, timeout=3600)
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