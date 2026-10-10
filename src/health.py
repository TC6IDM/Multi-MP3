"""Notice when yt-dlp has fallen behind a site, before it fails every track.

YouTube and SoundCloud change their sites without warning, and an outdated
yt-dlp then fails track after track ("Unable to extract …", "Requested format
is not available", YouTube 403s) until it's updated — a fix usually ships
within days. Each run's failure rate per provider is kept in
.cache/health.json; when the latest run fails far more than that provider
usually does, the run summary, the notification and the System tab say so and
point at the one-click update.

Only failures that survive every retry count. Rate limits are the limiter's
business, and tracks gone at the source (removed, private, region-locked)
aren't yt-dlp's fault.
"""

from __future__ import annotations

import json
import re
import statistics
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any

HISTORY = 20            # runs kept per provider
MIN_TRACKS = 5          # fewer attempts than this say nothing either way
ALERT_RATE = 0.2        # a fifth of the tracks failing...
USUAL_FACTOR = 2.0      # ...and at least twice the usual rate

LABELS = {"youtube": "YouTube", "soundcloud": "SoundCloud", "spotify": "Spotify"}

# What an extractor that no longer understands the site tends to say.
_BREAKAGE = re.compile(
    r"unable to extract|failed to extract|signature|nsig|n challenge|player response|"
    r"requested format is not available|no video formats|http error 403|"
    r"unsupported url|jsinterp|unable to download (api|webpage|json)|keyerror|"
    r"audioprovidererror|yt-dlp download error", re.I)

_lock = threading.Lock()


def _path(output_dir: Path) -> Path:
    return output_dir / ".cache" / "health.json"


def _load(output_dir: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        data = json.loads(_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(output_dir: Path, provider: str, downloaded: int, failures: list[str]) -> None:
    """Add one run's outcome for a provider (no-op when it fetched nothing)."""
    if downloaded + len(failures) == 0:
        return
    with _lock:
        data = _load(output_dir)
        runs = data.setdefault(provider, [])
        runs.append({
            "at": time.time(), "downloaded": downloaded, "failed": len(failures),
            "breakage": sum(1 for f in failures if _BREAKAGE.search(f)),
            "sample": [f[:200] for f in failures[:3]],
        })
        del runs[:-HISTORY]
        path = _path(output_dir)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass


def _rate(run: dict[str, Any]) -> float | None:
    total = int(run.get("downloaded", 0)) + int(run.get("failed", 0))
    return int(run.get("failed", 0)) / total if total >= MIN_TRACKS else None


def version_age_days(version: str | None, today: date | None = None) -> int | None:
    """yt-dlp versions are release dates: 2026.8.19 is 19 August 2026."""
    m = re.match(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not m:
        return None
    try:
        released = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    return ((today or date.today()) - released).days


def assess(output_dir: Path, ytdlp: str | None = None, spotdl_ytdlp: str | None = None,
           providers: list[str] | None = None) -> list[dict[str, Any]]:
    """Providers whose latest run failed far more than usual, with advice."""
    data = _load(output_dir)
    out: list[dict[str, Any]] = []
    for provider, runs in data.items():
        if providers is not None and provider not in providers:
            continue
        if not runs:
            continue
        last = runs[-1]
        rate = _rate(last)
        if rate is None or rate < ALERT_RATE:
            continue
        earlier = [r for r in (_rate(x) for x in runs[:-1]) if r is not None]
        usual = statistics.median(earlier) if earlier else None
        if usual is not None and rate < max(USUAL_FACTOR * usual, usual + 0.1):
            continue
        label = LABELS.get(provider, provider)
        failed = int(last.get("failed", 0))
        total = failed + int(last.get("downloaded", 0))
        msg = f"{label}: {failed} of {total} tracks failed in the last run ({rate:.0%}"
        msg += f", usually {usual:.0%})." if usual is not None else ")."
        # spotdl downloads through its own copy of yt-dlp.
        version = spotdl_ytdlp if provider == "spotify" else ytdlp
        age = version_age_days(version)
        if int(last.get("breakage", 0)) * 2 >= failed:
            msg += " The errors look like yt-dlp no longer understanding the site."
        if version and age is not None and age > 14:
            msg += f" yt-dlp {version} is {age} days old — updating it (System → Update yt-dlp) usually fixes this."
        else:
            msg += " Updating yt-dlp (System → Update yt-dlp) usually fixes this."
        out.append({"provider": provider, "rate": round(rate, 3),
                    "usual": round(usual, 3) if usual is not None else None,
                    "failed": failed, "total": total, "at": last.get("at"),
                    "sample": last.get("sample") or [], "message": msg})
    return out
