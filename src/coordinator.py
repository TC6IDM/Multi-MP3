from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
import json
import logging
import threading
from typing import Any, Dict, List

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
        self.output_dir = output_dir.resolve()
        self.logger = logger
        self.spotify_client_id = spotify_client_id
        self.spotify_client_secret = spotify_client_secret
        self.parallel = parallel
        self.max_workers = max_workers
        self.tui = progress or DownloadProgress(enabled=use_tui)
        self._downloaders: Dict[str, BaseDownloader] = {}
        self._downloader_lock = threading.Lock()

    def _build_downloader(self, provider: str) -> BaseDownloader:
        if provider == "spotify":
            return SpotifyDownloader(self.output_dir, self.logger,
                                     self.spotify_client_id, self.spotify_client_secret)
        elif provider == "soundcloud":
            return SoundCloudDownloader(self.output_dir, self.logger)
        elif provider == "youtube":
            return YouTubeDownloader(self.output_dir, self.logger)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    def _get_downloader(self, provider: str) -> BaseDownloader:
        """Return the cached downloader for a provider, creating it once.

        Rebuilding SpotifyDownloader per link re-created SpotifyClientCredentials
        every time, which re-runs the client-credentials token exchange.
        """
        d = self._downloaders.get(provider)
        if d is not None:
            return d
        with self._downloader_lock:
            d = self._downloaders.get(provider)
            if d is None:
                d = self._build_downloader(provider)
                self._downloaders[provider] = d
            return d

    # ── Phase 1: Download only (no cleanup) ──────────────────────────

    def _download_single_link(self, provider: str, link: str,
                               index: int, total: int) -> LinkResult:
        """Download one link — no cleanup. Safe to run concurrently."""
        downloader = self._get_downloader(provider)
        short_link = link.split("?")[0].split("/")[-1] or link.split("?")[0]

        # Progress callback is passed per download call, so a shared downloader
        # instance can serve several concurrent links without them clobbering
        # one another's callback.
        def _forward_progress(evt: str, data: dict) -> None:
            self.tui.track_progress(provider, link, evt, data)

        # ── Pre-fetch metadata for richer link_started event ──────────
        playlist_name = short_link
        total_tracks = 0
        track_list: List[Dict[str, Any]] = []
        if provider == "spotify":
            # Name resolution and track-list parsing are kept in separate try
            # blocks. They used to share one, so a failure midway (pagination
            # returning HTTP 400, for instance) discarded the entire track list
            # and the playlist rendered with no expandable songs.
            try:
                playlist_name = downloader.fetch_metadata_image(link) or short_link
            except Exception as e:
                self.logger.warning(f"Spotify metadata fetch failed: {e}")

            try:
                safe_name = "".join(
                    c for c in playlist_name if c.isalnum() or c in (' ', '-', '_')
                ).rstrip()
                meta_path = self.output_dir / ".metadata" / f"{safe_name}.json"
                if meta_path.is_file():
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    # Normalize: Spotify wraps playlist tracks in .track, albums are direct
                    raw_items = meta.get("tracks", {}).get("items", [])
                    total_tracks = meta.get("tracks", {}).get("total", len(raw_items))
                    for pos, item in enumerate(raw_items, 1):
                        t = item.get("track") or item  # playlist → .track, album → direct
                        if not isinstance(t, dict):
                            continue
                        track_list.append({
                            # Playlist position, not the album track number —
                            # the latter repeats across albums (1, 1, 1, 3, ...).
                            "num": pos,
                            "title": t.get("name", ""),
                            "artists": [a.get("name", "") for a in t.get("artists", []) if a],
                            "duration_ms": t.get("duration_ms", 0),
                            # spotdl reports failures by Spotify track URL, so
                            # the id is what lets us mark the right row failed.
                            # Local files added to a playlist have no id.
                            "id": t.get("id") or "",
                            "local": bool(item.get("is_local") or t.get("is_local")),
                        })
                    self.logger.info(
                        f"📊 {playlist_name}: {total_tracks} tracks "
                        f"({len(track_list)} listed)")
            except Exception as e:
                self.logger.warning(f"Track list parse failed: {e}")

        # Emit link started with real name + track count when available
        self.tui.link_started(provider, link, playlist_name, total_tracks)
        if track_list:
            self.tui.link_metadata(provider, link, track_list)

        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"[{provider.upper()} {index}/{total}] {short_link}")

        try:
            code, errors_file = downloader.download(link, on_progress=_forward_progress)
            if code != 0:
                self.logger.warning(f"Failed (code {code})")
            # For non-Spotify, fetch metadata after download
            if provider != "spotify":
                playlist_name = downloader.fetch_metadata_image(link) or playlist_name
            elif track_list:
                # spotdl commits to one YouTube candidate per track and gives up
                # if it fails, so retry those by searching YouTube ourselves.
                self._retry_failed_spotify(
                    downloader, playlist_name, track_list,
                    errors_file, _forward_progress,
                )
        except Exception as e:
            self.logger.error(f"💥 {provider} error on {link}: {e}")
            code = 1

        # Emit link complete
        self.tui.link_complete(provider, link, playlist_name, code)

        return LinkResult(
            provider=provider, link=link, playlist_name=playlist_name,
            code=code, index=index, total=total
        )

    # ── Recovery pass ────────────────────────────────────────────────

    def _retry_failed_spotify(self, downloader: BaseDownloader, playlist_name: str,
                              track_list: List[Dict[str, Any]], errors_file: Path,
                              emit: Any) -> None:
        """Re-attempt tracks spotdl couldn't fetch, via a direct YouTube search.

        spotdl resolves each track to a single YouTube URL and reports failure
        if that one is unavailable (age-gated, region-locked, taken down). A
        plain search usually turns up a working upload of the same song.
        """
        failed_ids = downloader._parse_spotdl_errors(errors_file)
        if not failed_ids:
            return

        by_id = {t["id"]: t for t in track_list if t.get("id")}
        padding = max(2, len(str(len(track_list))))
        # Filenames live under the playlist directory spotdl created
        safe_dir = "".join(
            c for c in playlist_name if c not in '<>:"/\\|?*'
        ).strip() or playlist_name

        targets = [(tid, by_id[tid]) for tid in failed_ids if tid in by_id]
        if not targets:
            return

        # Route retry output to the owning playlist's log pane rather than the
        # global one, matching how subprocess output is handled.
        def say(msg: str) -> None:
            if emit:
                emit("log", {"line": msg})
            else:
                self.logger.info(msg)

        say(f"🔁 {len(targets)} track(s) failed — retrying via YouTube search...")

        recovered = 0
        for tid, t in targets:
            title = t.get("title") or ""
            artists = t.get("artists") or []
            if not title:
                continue
            if emit:
                emit("track_retry", {"track_id": tid, "title": title})
            try:
                ok = downloader.search_and_download(
                    title=title, artists=artists, playlist_dir=safe_dir,
                    position=t.get("num"), padding=padding, on_log=say,
                )
            except Exception as e:
                say(f"Retry crashed for {title}: {e}")
                ok = False

            if ok:
                recovered += 1
                if emit:
                    emit("track_complete", {"title": title, "recovered": True})
            elif emit:
                emit("track_failed", {
                    "track_id": tid, "title": title,
                    "error": "No working source found on Spotify's match or YouTube",
                })

        say(f"🔁 Recovered {recovered}/{len(targets)} previously failed track(s)")
        # One line in the global log so the run summary still reflects it
        self.logger.info(
            f"🔁 {playlist_name}: recovered {recovered}/{len(targets)} failed track(s)")

    # ── Phase 2: Cleanup (runs after all downloads complete) ──────────

    def _cleanup_results(self, results: List[LinkResult]) -> None:
        """Run cleanup once for a completed batch.

        YouTube and SoundCloud cleanup are both whole-directory sweeps, so
        running one per provider scanned and rewrote every playlist twice —
        and, because providers run concurrently, two sweeps could hit the same
        .metadata/<name>.json at once. Do a single sweep instead, and give
        Spotify its per-playlist pass (it needs the playlist name).
        """
        providers = {r.provider for r in results}

        # Spotify: per-playlist, keyed by the name resolved during download
        if "spotify" in providers:
            downloader = self._get_downloader("spotify")
            for r in results:
                if r.provider != "spotify" or not r.playlist_name:
                    continue
                try:
                    downloader.cleanup(r.playlist_name)
                except Exception as e:
                    self.logger.error(f"💥 Cleanup error (spotify/{r.playlist_name}): {e}")

        # YouTube + SoundCloud: one shared directory sweep for both
        sweepers = [p for p in ("soundcloud", "youtube") if p in providers]
        if sweepers:
            # SoundCloud additionally clears stray root-level .info.json files,
            # so prefer it as the sweeper when present.
            provider = sweepers[0]
            try:
                self._get_downloader(provider).cleanup("")
            except Exception as e:
                self.logger.error(f"💥 Cleanup error ({provider}): {e}")

    # ── Per-provider processing ──────────────────────────────────────

    def process_provider(self, provider: str, links: List[str]) -> List[LinkResult]:
        """Download every link for one provider. Cleanup happens later, once
        all providers have finished, so concurrent sweeps cannot race."""
        if not links:
            self.logger.info(f"ℹ️ No {provider} links")
            return []

        self.logger.info(f"🎯 Starting {len(links)} {provider} links...")

        if self.parallel and len(links) > 1:
            results = self._download_parallel(provider, links)
        else:
            results = self._download_sequential(provider, links)

        exit_code = max((r.code for r in results), default=0)
        self.logger.info(f"✅ {provider} downloads done (exit: {exit_code})")
        return results

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

        all_results: List[LinkResult] = []

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
                        results = future.result()
                        all_results.extend(results)
                        code = max((r.code for r in results), default=0)
                        if code != 0:
                            exit_code = code
                    except Exception as e:
                        self.logger.error(f"💥 {provider} provider crashed: {e}")
                        exit_code = 1

            # Single cleanup barrier once every provider has finished writing.
            # Doing this per provider meant the output tree was swept once per
            # provider, concurrently, on the same metadata files.
            if all_results:
                self.logger.info("🧹 Running cleanup...")
                self._cleanup_results(all_results)

        self.logger.info(f"🏁 All providers complete (exit: {exit_code})")
        return exit_code
