from pathlib import Path
from typing import List, Tuple

from src.downloaders.base import BaseDownloader
from src.models import Song


class YouTubeDownloader(BaseDownloader):
    def download(self, link: str) -> Tuple[int, Path]:
        """Download YouTube playlist/channel via yt-dlp."""
        cmd = [
            "yt-dlp",
            "--extract-audio", "--audio-format", "mp3",
            "--yes-playlist",
            "--ignore-errors",
            "--no-abort-on-error",
            "--embed-thumbnail",
            "--write-info-json",
            "--add-metadata",
            "--audio-quality", "1",
            "--js-runtimes", "deno",
            "--output", "%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.%(ext)s",
            link,
        ]
        return self._download("yt-dlp", link, cmd)

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Cleanup metadata and scan for missing tracks across all playlists."""
        return self._cleanup_all_playlists()

    def fetch_metadata_image(self, url: str) -> str | None:
        return "yt-dlp-playlist"
