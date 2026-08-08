from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import logging
from typing import Dict, List

from src.downloaders.base import BaseDownloader
from src.utils import read_links
from src.downloaders.spotify import SpotifyDownloader
from src.downloaders.soundcloud import SoundCloudDownloader
from src.downloaders.youtube import YouTubeDownloader
from src.tui import DownloadProgress


class Coordinator:
    """Orchestrates downloading across Spotify, YouTube, and SoundCloud."""

    def __init__(self, output_dir: Path, logger: logging.Logger,
                 spotify_client_id: str, spotify_client_secret: str,
                 parallel: bool = False, max_workers: int = 4,
                 use_tui: bool = False):
        self.output_dir = output_dir
        self.logger = logger
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.parallel = parallel
        self.max_workers = max_workers
        self.tui = DownloadProgress(enabled=use_tui)

    def _get_downloader(self, provider: str) -> BaseDownloader:
        """Factory for provider-specific downloaders."""
        if provider == "spotify":
            return SpotifyDownloader(self.output_dir, self.logger,
                                     self.spotify_client_id, self.spotify_client_secret)
        elif provider == "soundcloud":
            return SoundCloudDownloader(self.output_dir, self.logger)
        elif provider == "youtube":
            return YouTubeDownloader(self.output_dir, self.logger)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _process_single_link(self, provider: str, link: str, index: int, total: int) -> int:
        """Download a single link and run cleanup. Used for both sequential and parallel modes."""
        downloader = self._get_downloader(provider)

        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"[{provider.upper()} {index}/{total}] {link.split('?')[0]}")

        try:
            code, errors_file = downloader.download(link)
            if code != 0:
                self.logger.warning(f"Failed (code {code})")

            playlist_name = downloader.fetch_metadata_image(link)
            downloader.cleanup(playlist_name)
            return code
        except Exception as e:
            self.logger.error(f"💥 {provider} error on {link}: {e}")
            return 1

    def process_provider(self, provider: str, links: List[str]) -> int:
        """Process one provider's links, optionally in parallel."""
        if not links:
            self.logger.info(f"ℹ️ No {provider} links")
            return 0

        self.logger.info(f"🎯 Starting {len(links)} {provider} links...")

        if self.parallel and len(links) > 1:
            return self._process_parallel(provider, links)
        else:
            return self._process_sequential(provider, links)

    def _process_sequential(self, provider: str, links: List[str]) -> int:
        """Download links one at a time."""
        exit_code = 0
        total = len(links)
        task_name = f"{provider} ({total} links)"
        self.tui.add_task(task_name, total=total)

        for i, link in enumerate(links, 1):
            short_link = link.split("?")[0].split("/")[-1] or link.split("?")[0]
            self.tui.update_description(task_name, f"[cyan]{provider}[/] {short_link}")
            code = self._process_single_link(provider, link, i, total)
            self.tui.advance(task_name)
            if code != 0:
                exit_code = code
        self.logger.info(f"✅ {provider} complete (exit: {exit_code})")
        return exit_code

    def _process_parallel(self, provider: str, links: List[str]) -> int:
        """Download links concurrently within the same provider."""
        exit_code = 0
        total = len(links)
        indexed_links = [(i + 1, link) for i, link in enumerate(links)]
        task_name = f"{provider} ({total} links)"
        self.tui.add_task(task_name, total=total)

        with ThreadPoolExecutor(max_workers=min(self.max_workers, total)) as executor:
            futures = {
                executor.submit(self._process_single_link, provider, link, idx, total): link
                for idx, link in indexed_links
            }
            for future in as_completed(futures):
                try:
                    code = future.result()
                    self.tui.advance(task_name)
                    if code != 0:
                        exit_code = code
                except Exception as e:
                    self.logger.error(f"💥 Parallel task crashed: {e}")
                    exit_code = 1

        self.logger.info(f"✅ {provider} complete (exit: {exit_code})")
        return exit_code

    def process_all(self, input_file: Path, providers: List[str] | None = None) -> int:
        """Unified entry: process all requested providers."""
        if providers is None:
            providers = ["soundcloud", "youtube", "spotify"]

        links_by_provider = read_links(input_file, self.logger)
        exit_code = 0

        with self.tui:
            for provider in providers:
                provider_exit = self.process_provider(provider, links_by_provider.get(provider, []))
                if provider_exit != 0:
                    exit_code = provider_exit

        return exit_code
