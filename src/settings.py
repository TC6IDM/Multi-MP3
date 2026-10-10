"""Speed presets and the environment overrides that tune each provider lane.

Every provider runs in its own lane, concurrently with the others, with its own
worker count and (for the yt-dlp engine) its own rate limit. The old single
"parallel" switch couldn't express that: turning it off to keep SoundCloud
under its rate limit also slowed Spotify and YouTube to one track at a time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

SPEEDS = ("careful", "balanced", "fast")
DEFAULT_SPEED = "balanced"


@dataclass(frozen=True)
class Lane:
    workers: int        # tracks (engine) or playlists (Spotify) in flight at once
    rate: float         # starting tracks/minute — the limiter adapts from here
    min_rate: float
    max_rate: float
    threads: int = 0    # spotdl --threads (Spotify only)


# The SoundCloud numbers come from yt-dlp's own estimate of its limit (~600 API
# requests per 10 minutes) and a track costing ~2–3 of those, so ~20–30 tracks
# a minute is the real ceiling. The limiter finds the actual edge and remembers
# it, so these only matter for the very first run.
_BALANCED = {
    "soundcloud": Lane(workers=3, rate=20, min_rate=4, max_rate=40),
    "youtube": Lane(workers=3, rate=30, min_rate=6, max_rate=60),
    "spotify": Lane(workers=2, rate=0, min_rate=0, max_rate=0, threads=6),
}
_CAREFUL = {
    "soundcloud": Lane(workers=1, rate=8, min_rate=2, max_rate=12),
    "youtube": Lane(workers=1, rate=15, min_rate=4, max_rate=20),
    "spotify": Lane(workers=1, rate=0, min_rate=0, max_rate=0, threads=3),
}


def _env_num(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def env_rate_override(provider: str) -> float | None:
    """The explicitly configured starting rate, if any (beats the learned one)."""
    raw = os.getenv(f"{provider.upper()}_RATE", "").strip()
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def lane(provider: str, speed: str = DEFAULT_SPEED, max_workers: int | None = None) -> Lane:
    """Resolve one provider's lane for a speed preset.

    Environment overrides (SOUNDCLOUD_WORKERS, SOUNDCLOUD_RATE, …) tune the
    balanced preset, and fast is derived from it. Careful ignores them — it is
    the fallback for when the tuned settings are getting you rate-limited.
    """
    speed = speed if speed in SPEEDS else DEFAULT_SPEED
    if speed == "careful":
        result = _CAREFUL[provider]
    else:
        p = provider.upper()
        base = _BALANCED[provider]
        result = replace(
            base,
            workers=int(_env_num(f"{p}_WORKERS", base.workers)) or base.workers,
            rate=_env_num(f"{p}_RATE", base.rate),
            min_rate=_env_num(f"{p}_MIN_RATE", base.min_rate),
            max_rate=_env_num(f"{p}_MAX_RATE", base.max_rate),
            threads=int(_env_num(f"{p}_THREADS", base.threads)),
        )
        if speed == "fast":
            result = replace(
                result,
                workers=result.workers + max(1, result.workers // 2),
                rate=result.rate * 1.5,
                max_rate=result.max_rate * 1.5,
                threads=result.threads + 2 if result.threads else 0,
            )
    if max_workers:
        result = replace(result, workers=max(1, int(max_workers)))
    if result.max_rate:
        result = replace(result, rate=min(max(result.rate, result.min_rate), result.max_rate))
    return result


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except ValueError:
        return default
