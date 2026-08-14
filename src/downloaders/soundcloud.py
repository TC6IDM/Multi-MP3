import hashlib
import os
import re
import time
import urllib.request
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

    # A run that dies before its first track is a different failure: nothing
    # was rate-limited, the tool simply never got going (usually the client_id
    # lookup timing out). Waiting minutes for that helps nobody.
    STARTUP_BACKOFF = (10, 30, 60)

    # Where SoundCloud's web player hides the anonymous API key: the homepage
    # links a handful of JS bundles, one of which declares client_id:"...".
    # Same two patterns the `soundcloud` library uses.
    _ASSET_SCRIPT_RE = re.compile(r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"')
    _CLIENT_ID_RE = re.compile(r'client_id:"([^"]+)"')
    _BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    # A generated id keeps working for days, but re-deriving one is cheap next
    # to a dead run, so the cache is refreshed daily rather than held forever.
    CLIENT_ID_TTL = 24 * 3600

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

        return self._download_with_resume(link, cmd, archive, on_progress)

    def _download_with_resume(self, link: str, base_cmd: List[str], archive: Path,
                              on_progress: Callable[[str, Dict[str, Any]], None] | None,
                              ) -> Tuple[int, Path]:
        """Run scdl, re-running it from where it stopped if tracks failed.

        A rate-limited run doesn't crash — it exits 0 having failed every track
        after the wall was hit. So the decision to resume is driven by the
        per-item error count the output parser collects, not the return code.

        A run that never reached a track is retried too, on a much shorter
        clock: that failure mode is scdl's startup, not the rate limit.
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
        refresh_client_id = False

        for attempt in range(1, max_attempts + 1):
            before = self._archive_count(archive)
            # Nothing to announce as a resume when the last attempt never got
            # going — refresh_client_id is exactly that case, and it already
            # logged why it is going round again.
            if attempt > 1 and not refresh_client_id:
                # Reported as a count, not a position: a rate limit usually
                # kills a contiguous tail, but a scattered failure would make
                # "resuming from track N" a lie.
                say(f"▶️ Resuming — {before} track(s) already done, skipping those "
                    f"(attempt {attempt}/{max_attempts})")

            cmd = list(base_cmd)
            client_id = self._client_id(refresh=refresh_client_id, say=say)
            if client_id:
                cmd += ["--client-id", client_id]

            stats: Dict[str, Any] = {}
            code, errors_file = self._download(
                "scdl", link, cmd, on_progress=on_progress, stats_out=stats)

            after = self._archive_count(archive)
            failed = int(stats.get("errors", 0) or 0)
            gained = after - before
            # Did scdl get as far as the playlist at all? Any of these means
            # yes; none of them with a non-zero exit means it died on startup.
            # A re-run of a finished playlist reaches only the archive skips,
            # so those count as having started too.
            started = (bool(stats.get("total")) or gained > 0 or failed > 0
                       or bool(stats.get("archived")))

            if not failed:
                if started or code == 0:
                    if attempt > 1:
                        say(f"✅ Resume complete — {after} track(s) downloaded in total")
                    return code, errors_file

                # Nothing downloaded, nothing even attempted. scdl derives an
                # API client_id at startup by pulling soundcloud.com under a
                # fixed 30s timeout, and a slow response there kills the whole
                # run before track one (curl error 28). Drop the cached id and
                # try again shortly — this clears on its own, unlike a limit.
                if attempt >= max_attempts:
                    say(f"❌ scdl exited {code} without starting the playlist after "
                        f"{attempt} attempt(s). Check the link is reachable, or set "
                        f"SOUNDCLOUD_CLIENT_ID to skip the lookup.")
                    break
                refresh_client_id = True
                wait = self.STARTUP_BACKOFF[min(attempt - 1, len(self.STARTUP_BACKOFF) - 1)]
                say(f"⚠️ scdl exited {code} before downloading anything — retrying in "
                    f"{wait}s with a freshly fetched client id.")
                self._sleep(wait)
                continue

            refresh_client_id = False

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

    def _client_id(self, refresh: bool = False,
                   say: Callable[[str], None] | None = None) -> str:
        """A SoundCloud API client_id for scdl, or "" to let scdl find its own.

        scdl derives one at startup by downloading soundcloud.com plus its JS
        bundles, all under curl's fixed 30s timeout. That homepage is heavy, so
        on a slow or congested link the request dies mid-body ("curl: (28)
        Operation timed out ... with 557020 bytes received") and takes the
        whole playlist with it — before a single track.

        Doing the lookup here fixes both halves of that: our own timeout is
        generous and configurable, and the answer is cached on disk, so the
        usual run hands scdl a ready client_id and never touches soundcloud.com
        at startup at all. If we can't get one either, we return "" and scdl
        behaves exactly as it does today.
        """
        env_id = os.getenv("SOUNDCLOUD_CLIENT_ID", "").strip()
        if env_id:
            return env_id

        cache = self.output_dir / ".cache" / "soundcloud-client-id.txt"
        if refresh:
            # The cached id is the prime suspect when a run dies on startup:
            # scdl falls back to generating one when it's rejected, which is
            # the very lookup that just timed out.
            cache.unlink(missing_ok=True)
        else:
            try:
                age = time.time() - cache.stat().st_mtime
                cached = cache.read_text(encoding="utf-8").strip()
                if cached and age < self.CLIENT_ID_TTL:
                    return cached
            except OSError:
                pass

        client_id = self._generate_client_id()
        if not client_id:
            msg = ("⚠️ Could not pre-fetch a SoundCloud client id; letting scdl "
                   "try its own lookup.")
            (say or self.logger.warning)(msg)
            return ""

        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(client_id, encoding="utf-8")
        except OSError as e:
            self.logger.debug(f"Could not cache SoundCloud client id: {e}")
        self.logger.info("🔑 Resolved SoundCloud client id")
        return client_id

    def _generate_client_id(self) -> str:
        """Scrape a client_id out of the web player's JS bundles."""
        timeout = self._env_int("SOUNDCLOUD_CLIENT_ID_TIMEOUT", 90)
        try:
            home = self._http_get("https://soundcloud.com", timeout)
        except Exception as e:
            self.logger.warning(f"SoundCloud client id lookup failed: {e}")
            return ""

        # Newest bundles come last and are the ones that carry the key, so the
        # list is walked backwards — usually a single request instead of ten.
        for url in reversed(self._ASSET_SCRIPT_RE.findall(home)):
            try:
                script = self._http_get(url, timeout)
            except Exception:
                continue
            m = self._CLIENT_ID_RE.search(script)
            if m:
                return m.group(1)
        return ""

    def _http_get(self, url: str, timeout: int) -> str:
        """Fetch a page as a browser would, with our own timeout.

        Prefers curl_cffi (scdl's own HTTP stack) because SoundCloud serves a
        different, TLS-fingerprinted response to plain Python clients; falls
        back to urllib where it isn't installed.
        """
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            curl_requests = None

        if curl_requests is not None:
            r = curl_requests.get(url, timeout=timeout, impersonate="chrome")
            r.raise_for_status()
            return r.text

        req = urllib.request.Request(url, headers={
            "User-Agent": self._BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

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
