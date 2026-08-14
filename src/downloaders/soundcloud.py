import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from urllib.parse import urlparse

from src.downloaders.base import BaseDownloader
from src.models import Song


class SoundCloudDownloader(BaseDownloader):
    # How many times a partially-failed playlist is resumed before giving up,
    # and how long to wait before each resume. The waits are long on purpose:
    # a partial failure here almost always means the IP hit SoundCloud's rate
    # limit, and that only clears with time — retrying immediately just burns
    # the next attempt too.
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_BACKOFF = (60, 300, 900)

    def _archive_path(self, link: str) -> Path:
        """Where the download archive for one playlist lives.

        Keyed on the URL path only, so a private link's ?secret_token= doesn't
        strand the playlist with a second archive. The hash disambiguates
        paths that collide once slugified.
        """
        path = urlparse(link).path.strip("/").lower()
        slug = re.sub(r"[^a-z0-9]+", "-", path).strip("-") or "playlist"
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:8]
        return self.output_dir / ".archive" / f"{slug[:60]}-{digest}.txt"

    @staticmethod
    def _archive_count(archive: Path) -> int:
        """How many tracks the archive has recorded as done."""
        try:
            return sum(1 for line in archive.read_text(encoding="utf-8").splitlines()
                       if line.strip())
        except OSError:
            return 0

    def download(self, link: str,
                 on_progress: Callable[[str, Dict[str, Any]], None] | None = None
                 ) -> Tuple[int, Path]:
        """Download SoundCloud link via scdl CLI, resuming if it fails partway."""
        # SoundCloud rate-limits by IP. Once tripped it rejects the cached
        # client_id AND blocks the request yt-dlp makes to refresh it, so every
        # remaining track fails with 403 — a 460-track playlist died at ~102.
        # Pacing requests and backing off on extractor errors keeps runs under
        # the limit; the sleep is per track, so long playlists take longer but
        # actually finish.
        #
        # Within a playlist everything is deliberately serial — one request in
        # flight at a time. scdl walks the playlist through a single yt-dlp
        # session, so tracks are already sequential; --concurrent-fragments 1
        # keeps a single track from fanning out into parallel HLS range
        # requests, and --sleep-requests spaces out the metadata calls that
        # actually carry the rate limit. Turning off --parallel serialises the
        # layer above this (providers and playlists).
        yt_dlp_args = (
            "--write-info-json --ignore-errors --no-abort-on-error --yes-playlist "
            "--embed-thumbnail --audio-quality 1 "
            "--concurrent-fragments 1 --sleep-requests 1 "
            "--sleep-interval 1 --max-sleep-interval 4 "
            "--retries 5 --extractor-retries 5 --retry-sleep exp=2:60"
        )

        # The download archive is what makes resuming cheap. Without it a
        # resumed run still calls the /tracks/<id> API for every track it
        # already has, just to work out the filename and discover it exists —
        # 102 wasted requests against the very limit that killed the run.
        # yt-dlp checks the archive against the playlist entry (which already
        # carries the track id from the single playlist call), so archived
        # tracks cost no requests at all. Only successfully downloaded tracks
        # are recorded, so failures are always retried.
        archive = self._archive_path(link)
        archive.parent.mkdir(parents=True, exist_ok=True)

        cmd = [
            "scdl",
            "-l", link,
            "--path", str(self.output_dir.resolve()),
            "--download-archive", str(archive),
            # scdl already creates a directory named after the playlist under
            # --path, so a leading %(playlist)s/ here nested it twice:
            #   downloads/<playlist>/<playlist>/0001 ....mp3
            # which also hid the files from the cleanup/missing-track scan.
            "--playlist-name-format", "%(playlist_index)04d %(uploader)s - %(title)s.%(ext)s",
            "--onlymp3",
            "--original-art",
            "-c",
            "--yt-dlp-args", yt_dlp_args,
        ]

        # An authenticated session gets far higher rate limits. Optional —
        # without it we simply rely on the pacing above.
        auth_token = os.getenv("SOUNDCLOUD_AUTH_TOKEN", "").strip()
        if auth_token:
            cmd += ["--auth-token", auth_token]
            self.logger.info("🔑 Using SoundCloud auth token")
        client_id = os.getenv("SOUNDCLOUD_CLIENT_ID", "").strip()
        if client_id:
            cmd += ["--client-id", client_id]

        return self._download_with_resume(link, cmd, archive, on_progress)

    def _download_with_resume(self, link: str, cmd: List[str], archive: Path,
                              on_progress: Callable[[str, Dict[str, Any]], None] | None,
                              ) -> Tuple[int, Path]:
        """Run scdl, re-running it from where it stopped if tracks failed.

        A rate-limited run doesn't crash — it exits 0 having failed every track
        after the wall was hit. So the decision to resume is driven by the
        per-item error count the output parser collects, not the return code.
        """
        max_attempts = self._env_int("SOUNDCLOUD_MAX_ATTEMPTS", self.DEFAULT_MAX_ATTEMPTS)
        backoff = self._env_backoff()

        def say(msg: str) -> None:
            # Route to the owning playlist's log pane when there is one, so a
            # long wait doesn't look like the run has silently frozen.
            if on_progress:
                on_progress("log", {"line": msg})
            self.logger.info(msg)

        code, errors_file = 1, self.errors_dir / "errors-scdl-unstarted.txt"

        for attempt in range(1, max_attempts + 1):
            before = self._archive_count(archive)
            if attempt > 1:
                # Reported as a count, not a position: a rate limit usually
                # kills a contiguous tail, but a scattered failure would make
                # "resuming from track N" a lie.
                say(f"▶️ Resuming — {before} track(s) already done, skipping those "
                    f"(attempt {attempt}/{max_attempts})")

            stats: Dict[str, Any] = {}
            code, errors_file = self._download(
                "scdl", link, cmd, on_progress=on_progress, stats_out=stats)

            after = self._archive_count(archive)
            failed = int(stats.get("errors", 0) or 0)
            gained = after - before

            if not failed:
                if attempt > 1:
                    say(f"✅ Resume complete — {after} track(s) downloaded in total")
                return code, errors_file

            if attempt >= max_attempts:
                say(f"⚠️ {failed} track(s) still failing after {attempt} attempt(s); "
                    f"{after} downloaded. Re-run this link later — it will skip "
                    f"those and retry the rest.")
                break

            # Two attempts in a row that download nothing new mean waiting
            # longer isn't helping — the tracks are gone, or the limit is far
            # from clearing. Either way, stop burning attempts.
            if gained == 0 and attempt > 1:
                say(f"⏹️ Resume made no progress twice in a row; stopping at "
                    f"{after} track(s).")
                break

            wait = backoff[min(attempt - 1, len(backoff) - 1)]
            say(f"⏳ {failed} track(s) failed after {gained} new this pass — "
                f"waiting {wait}s for the rate limit to clear, then resuming.")
            self._sleep(wait)

        return code, errors_file

    @staticmethod
    def _sleep(seconds: int) -> None:
        """Sleep in slices so a Ctrl-C during a long backoff still lands."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(5.0, remaining))

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, "").strip() or default))
        except ValueError:
            return default

    def _env_backoff(self) -> Tuple[int, ...]:
        raw = os.getenv("SOUNDCLOUD_RETRY_BACKOFF", "").strip()
        if not raw:
            return self.DEFAULT_BACKOFF
        try:
            waits = tuple(max(0, int(p)) for p in raw.split(",") if p.strip())
        except ValueError:
            self.logger.warning(
                f"Ignoring invalid SOUNDCLOUD_RETRY_BACKOFF={raw!r}")
            return self.DEFAULT_BACKOFF
        return waits or self.DEFAULT_BACKOFF

    def cleanup(self, playlist_name: str) -> List[Song]:
        """Cleanup metadata and scan for missing tracks across all playlists."""
        # Delete root-level .info.json files that scdl sometimes leaves
        for infofile in self.output_dir.glob("*.info.json"):
            self.logger.info(f"🗑️ Deleting root {infofile.name}")
            infofile.unlink(missing_ok=True)
        return self._cleanup_all_playlists()

    def fetch_metadata_image(self, url: str) -> str | None:
        return "scdl-playlist"
