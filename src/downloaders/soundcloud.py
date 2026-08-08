from pathlib import Path
from typing import List, Tuple

from src.downloaders.base import BaseDownloader
from src.models import Song


class SoundCloudDownloader(BaseDownloader):
    def download(self, link: str) -> Tuple[int, Path]:
        """Download SoundCloud link via scdl CLI."""
        cmd = [
            "scdl",
            "-l", link,
            "--path", str(self.output_dir),
            "--playlist-name-format", "%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.%(ext)s",
            "--onlymp3",
            "--original-art",
            "-c",
            "--debug",
            "--yt-dlp-args", "--write-info-json --ignore-errors --no-abort-on-error --yes-playlist --embed-thumbnail --audio-quality 1",
        ]
        return self._download("scdl", link, cmd)

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Cleanup metadata and scan for missing tracks across all playlists."""
        # Delete root-level .info.json files that scdl sometimes leaves
        for infofile in self.output_dir.glob("*.info.json"):
            self.logger.info(f"🗑️ Deleting root {infofile.name}")
            infofile.unlink(missing_ok=True)
        return self._cleanup_all_playlists()

    def fetch_metadata_image(self, url: str) -> str | None:
        return "scdl-playlist"
