import json
import logging
import os
import re
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp

from src.downloaders.ytdlp_engine import EngineProfile, YtDlpEngine
from src.ratelimit import AdaptiveRateLimiter
from src.settings import Lane, env_int


class SoundCloudDownloader:
    """SoundCloud through the in-process yt-dlp engine.

    This used to shell out to scdl once per playlist. scdl is itself a yt-dlp
    wrapper, so the engine reproduces its options exactly (format choice, album
    tags from the playlist, the `%(playlist)s/NNNN uploader - title` layout)
    while adding what scdl couldn't: one request budget shared across every
    playlist in the run, and an exact per-track record.
    """

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

    OUTTMPL = "%(playlist)s/%(playlist_index)04d %(uploader)s - %(title)s.%(ext)s"

    def __init__(self, output_dir: Path, logger: logging.Logger) -> None:
        self.output_dir = output_dir
        self.logger = logger

    def profile(self, lane: Lane, limiter: AdaptiveRateLimiter) -> EngineProfile:
        args = [
            # SoundCloud has been retiring its mp3 transcodings. scdl's
            # --onlymp3 failed outright on tracks without one; prefer mp3 (no
            # re-encode) and fall back to the AAC stream, converted below.
            "-f", "ba[format_id*=mp3]/ba",
            "--extractor-args", "soundcloud:formats=*_aac,*_mp3",
            "-x", "--audio-format", "mp3", "--audio-quality", "1",
            "--embed-metadata", "--embed-thumbnail",
            "--parse-metadata", "%(playlist)s:%(meta_album)s",
            "--parse-metadata", "%(playlist_uploader)s:%(meta_album_artist)s",
            "--parse-metadata", "%(playlist_index)s:%(meta_track)s",
            "--trim-filenames", "240",
            # One request in flight per track; the limiter spaces the tracks.
            "--concurrent-fragments", "1",
            "--retries", "5",
            # yt-dlp's own 429 retries would keep knocking on a closed door;
            # the shared limiter handles rate limits for every worker at once.
            "--extractor-retries", "0",
        ]
        # An authenticated session gets far higher rate limits. Optional.
        auth_token = os.getenv("SOUNDCLOUD_AUTH_TOKEN", "").strip()
        if auth_token:
            args += ["--username", "oauth", "--password", auth_token]
            self.logger.info("🔑 Using SoundCloud auth token")

        return EngineProfile(
            provider="soundcloud", label="SoundCloud", ydl_args=args, outtmpl=self.OUTTMPL,
            workers=lane.workers, limiter=limiter,
            max_attempts=env_int("SOUNDCLOUD_MAX_ATTEMPTS", 3),
            forbidden_is_rate_limit=True, legacy_archive=True, tags_from_playlist=True,
            before_run=self._seed_client_id, refresh_auth=self._refresh_client_id,
            backfill=self._backfill_titles,
        )

    # ── Engine hooks ─────────────────────────────────────────────────

    def _store_client_id(self, engine: YtDlpEngine, client_id: str) -> None:
        """Hand yt-dlp our client id through its own cache, which is where its
        SoundCloud extractor looks before scraping soundcloud.com itself."""
        with yt_dlp.YoutubeDL({"cachedir": str(engine.cachedir), "quiet": True}) as ydl:
            ydl.cache.store("soundcloud", "client_id", client_id)

    def _seed_client_id(self, engine: YtDlpEngine) -> None:
        client_id = self._client_id()
        if client_id:
            self._store_client_id(engine, client_id)

    def _refresh_client_id(self, engine: YtDlpEngine) -> bool:
        client_id = self._client_id(refresh=True)
        if client_id:
            self._store_client_id(engine, client_id)
        return bool(client_id)

    def _backfill_titles(self, engine: YtDlpEngine, tracks: list[dict[str, Any]]) -> None:
        """Fill in titles the playlist call didn't return.

        SoundCloud only sends full data for the first few tracks of a set; the
        rest arrive as bare ids, which would leave the track list reading
        "Track 6, Track 7, …" until each one downloads. A batched /tracks?ids=
        lookup fixes that for ~1 request per 50 tracks, and only on a
        playlist's first run — titles are kept in its record after that.
        """
        missing = [t for t in tracks if not t.get("title") and str(t.get("id") or "").isdigit()]
        if not missing:
            return
        client_id = self._client_id()
        if not client_id:
            return
        for start in range(0, len(missing), 50):
            if engine.cancel.is_set():
                return
            chunk = missing[start:start + 50]
            url = ("https://api-v2.soundcloud.com/tracks?ids="
                   + ",".join(str(t["id"]) for t in chunk) + f"&client_id={client_id}")
            try:
                data = json.loads(self._http_get(url, 30))
            except Exception as e:
                # Best effort: titles also arrive as each track downloads. A
                # 429 here is still news the limiter needs, though.
                if "429" in str(e):
                    engine.p.limiter.on_rate_limited(reason=str(e))
                self.logger.debug(f"SoundCloud title lookup stopped: {e}")
                return
            by_id = {str(d.get("id")): d for d in data if isinstance(d, dict)} if isinstance(data, list) else {}
            for t in chunk:
                d = by_id.get(str(t["id"]))
                if not d:
                    continue
                t["title"] = d.get("title") or t.get("title") or ""
                user = (d.get("user") or {}).get("username")
                if user:
                    t["artists"] = [user]
            time.sleep(1)   # a short burst at most — this isn't on the limiter's clock

    # ── client_id ────────────────────────────────────────────────────

    def _client_id(self, refresh: bool = False,
                   say: Callable[[str], None] | None = None) -> str:
        """A SoundCloud API client_id, or "" to let yt-dlp find its own.

        The id is scraped from soundcloud.com plus its JS bundles. That homepage
        is heavy, and under scdl's fixed 30s curl timeout a slow response died
        mid-body ("curl: (28) Operation timed out ... with 557020 bytes
        received"), taking the whole playlist with it before a single track.

        Doing the lookup here fixes both halves of that: our own timeout is
        generous and configurable, and the answer is cached on disk, so the
        usual run never touches soundcloud.com at startup at all.
        """
        env_id = os.getenv("SOUNDCLOUD_CLIENT_ID", "").strip()
        if env_id:
            return env_id

        cache = self.output_dir / ".cache" / "soundcloud-client-id.txt"
        if refresh:
            # The cached id is the prime suspect after a 403.
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
            msg = ("⚠️ Could not pre-fetch a SoundCloud client id; letting yt-dlp "
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
        timeout = env_int("SOUNDCLOUD_CLIENT_ID_TIMEOUT", 90)
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

        Prefers curl_cffi because SoundCloud serves a different, TLS-
        fingerprinted response to plain Python clients; falls back to urllib
        where it isn't installed.
        """
        curl_requests: Any
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            curl_requests = None

        if curl_requests is not None:
            r = curl_requests.get(url, timeout=timeout, impersonate="chrome")
            r.raise_for_status()
            return str(r.text)

        req = urllib.request.Request(url, headers={
            "User-Agent": self._BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(resp.read().decode("utf-8", "replace"))
