from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import logging
from typing import Dict, List

from src.downloaders.base import BaseDownloader
from src.utils import read_links
from src.downloaders.spotify import SpotifyDownloader
from src.downloaders.soundcloud import SoundCloudDownloader
from src.downloaders.youtube import YouTubeDownloader
from src.tui import DownloadProgress


@dataclass
class LinkResult:
    """Result of a single link download (before cleanup)."""
    provider: str
    link: str
    playlist_name: str
    code: int
    index: int = 0
    total: int = 0


class Coordinator:
    """Orchestrates downloading across Spotify, YouTube, and SoundCloud."""

    def __init__(self, output_dir: Path, logger: logging.Logger,
                 spotify_client_id: str, spotify_client_secret: str,
                 parallel: bool = False, max_workers: int = 4,
                 use_tui: bool = False,
                 progress: DownloadProgress | None = None):
        self.output_dir = output_dir
        self.logger = logger
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.parallel = parallel
        self.max_workers = max_workers
        self.tui = progress or DownloadProgress(enabled=use_tui)

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

    # ── Phase 1: Download only (no cleanup) ──────────────────────────

    def _download_single_link(self, provider: str, link: str,
                               index: int, total: int) -> LinkResult:
        """Download one link — no cleanup. Safe to run concurrently."""
        downloader = self._get_downloader(provider)

        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"[{provider.upper()} {index}/{total}] {link.split('?')[0]}")

        try:
            code, _ = downloader.download(link)
            if code != 0:
                self.logger.warning(f"Failed (code {code})")
            playlist_name = downloader.fetch_metadata_image(link) or ""
        except Exception as e:
            self.logger.error(f"💥 {provider} error on {link}: {e}")
            code = 1
            playlist_name = ""

        return LinkResult(
            provider=provider, link=link, playlist_name=playlist_name,
            code=code, index=index, total=total
        )

    # ── Phase 2: Cleanup (runs after all downloads complete) ──────────

    def _cleanup_results(self, results: List[LinkResult]) -> None:
        """Run cleanup on completed downloads. Provider-aware."""
        # YouTube and SoundCloud: bulk cleanup scans whole output dir
        providers_needing_cleanup = set(r.provider for r in results)
        for provider in providers_needing_cleanup:
            downloader = self._get_downloader(provider)
            # Use a dummy playlist name — YouTube/SoundCloud scan all dirs
            try:
                downloader.cleanup("")
            except Exception as e:
                self.logger.error(f"💥 Cleanup error ({provider}): {e}")

    # ── Per-provider processing ──────────────────────────────────────

    def process_provider(self, provider: str, links: List[str]) -> int:
        """Download all links for one provider, then cleanup."""
        if not links:
            self.logger.info(f"ℹ️ No {provider} links")
            return 0

        self.logger.info(f"🎯 Starting {len(links)} {provider} links...")

        if self.parallel and len(links) > 1:
            results = self._download_parallel(provider, links)
        else:
            results = self._download_sequential(provider, links)

        exit_code = max((r.code for r in results), default=0)

        # Barrier: all downloads done, now cleanup
        self.logger.info(f"🧹 Running {provider} cleanup...")
        self._cleanup_results(results)
        self.logger.info(f"✅ {provider} complete (exit: {exit_code})")
        return exit_code

    def _download_sequential(self, provider: str, links: List[str]) -> List[LinkResult]:
        """Download links one at a time (no cleanup)."""
        results: List[LinkResult] = []
        total = len(links)
        task_name = f"{provider} ({total} links)"
        self.tui.add_task(task_name, total=total)

        for i, link in enumerate(links, 1):
            short_link = link.split("?")[0].split("/")[-1] or link.split("?")[0]
            self.tui.update_description(task_name, f"[cyan]{provider}[/] {short_link}")
            result = self._download_single_link(provider, link, i, total)
            results.append(result)
            self.tui.advance(task_name)
        return results

    def _download_parallel(self, provider: str, links: List[str]) -> List[LinkResult]:
        """Download links concurrently within a provider (no cleanup)."""
        results: List[LinkResult] = []
        total = len(links)
        indexed_links = [(i + 1, link) for i, link in enumerate(links)]
        task_name = f"{provider} ({total} links)"
        self.tui.add_task(task_name, total=total)

        with ThreadPoolExecutor(max_workers=min(self.max_workers, total)) as executor:
            futures = {
                executor.submit(self._download_single_link, provider, link, idx, total): link
                for idx, link in indexed_links
            }
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    self.tui.advance(task_name)
                except Exception as e:
                    self.logger.error(f"💥 Parallel task crashed: {e}")
        return results

    # ── Cross-provider orchestration ─────────────────────────────────

    def process_all(self, input_file: Path, providers: List[str] | None = None) -> int:
        """Run all providers in parallel (download phases), then cleanup each."""
        if providers is None:
            providers = ["soundcloud", "youtube", "spotify"]

        links_by_provider = read_links(input_file, self.logger)
        active_providers = [(p, links_by_provider.get(p, [])) for p in providers if links_by_provider.get(p)]

        if not active_providers:
            self.logger.info("ℹ️ No links found for any provider")
            return 0

        exit_code = 0

        with self.tui:
            # Run all provider download phases in parallel
            with ThreadPoolExecutor(max_workers=min(len(active_providers), self.max_workers)) as executor:
                future_to_provider = {
                    executor.submit(self.process_provider, p, links): p
                    for p, links in active_providers
                }
                for future in as_completed(future_to_provider):
                    provider = future_to_provider[future]
                    try:
                        code = future.result()
                        if code != 0:
                            exit_code = code
                    except Exception as e:
                        self.logger.error(f"💥 {provider} provider crashed: {e}")
                        exit_code = 1

        return exit_code
