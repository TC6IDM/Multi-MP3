import logging
from pathlib import Path

from src.downloaders.ytdlp_engine import EngineProfile
from src.ratelimit import AdaptiveRateLimiter
from src.settings import Lane, env_int
from src.utils import deno_path


class YouTubeDownloader:
    """YouTube playlists through the in-process yt-dlp engine.

    Same options and filenames as the old `yt-dlp` subprocess, but tracks now
    download several at a time instead of strictly one after another, under a
    shared limiter that backs off when YouTube starts asking whether we're a bot.
    """

    OUTTMPL = "%(playlist_title)s/%(playlist_index)02d %(uploader)s - %(title)s.%(ext)s"

    def __init__(self, output_dir: Path, logger: logging.Logger) -> None:
        self.output_dir = output_dir
        self.logger = logger

    def profile(self, lane: Lane, limiter: AdaptiveRateLimiter) -> EngineProfile:
        # yt-dlp needs a JS runtime for YouTube's challenge — without one every
        # request is HTTP 403. Point it at the venv's deno explicitly, since the
        # venv's Scripts directory is only on PATH when activated.
        deno = deno_path()
        if not deno:
            self.logger.warning("⚠️ deno not found — YouTube downloads will likely fail with HTTP 403. "
                                "Install it with `pip install deno`.")
        args = [
            "-x", "--audio-format", "mp3", "--audio-quality", "1",
            "--embed-thumbnail", "--embed-metadata",
            "--js-runtimes", f"deno:{deno}" if deno else "deno",
            "--retries", "5",
            "--extractor-retries", "1",
        ]
        return EngineProfile(
            provider="youtube", label="YouTube", ydl_args=args, outtmpl=self.OUTTMPL,
            workers=lane.workers, limiter=limiter,
            max_attempts=env_int("YOUTUBE_MAX_ATTEMPTS", 3),
        )
