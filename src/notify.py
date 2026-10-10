"""Tell you when a run finishes, wherever you are.

The web UI shows a browser notification by itself. For a phone ping, set
NOTIFY_URL to:

  * an ntfy topic — https://ntfy.sh/<your-topic> (or your own ntfy server);
    install the ntfy app and subscribe to the same topic, or
  * a Discord webhook — https://discord.com/api/webhooks/…

Anything else is sent the same way as ntfy: a plain-text POST with a Title
header, which most webhook receivers accept.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request


def notify(title: str, message: str, logger: logging.Logger | None = None,
           url: str | None = None) -> bool:
    """Send one notification. Never raises — a failed ping mustn't fail a run."""
    target = (url if url is not None else os.getenv("NOTIFY_URL", "")).strip()
    if not target:
        return False
    try:
        if "discord.com/api/webhooks" in target or "discordapp.com/api/webhooks" in target:
            body = json.dumps({"content": f"**{title}**\n{message}"}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            body = message.encode("utf-8")
            # HTTP headers are latin-1; ntfy reads an RFC 2047-free title fine
            # as long as it has no emoji, so keep the header plain.
            headers = {"Title": title.encode("ascii", "ignore").decode() or "Multi-MP3",
                       "Content-Type": "text/plain; charset=utf-8"}
        req = urllib.request.Request(target, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except Exception as e:
        if logger:
            logger.warning(f"Notification to NOTIFY_URL failed: {e}")
        return False


def run_summary(status: str, counts: dict[str, int]) -> tuple[str, str]:
    """Title and body for a finished run."""
    done, total = counts.get("done", 0), counts.get("total", 0)
    failed, pending = counts.get("failed", 0), counts.get("pending", 0)
    title = {
        "completed": "Multi-MP3: all done",
        "partial": "Multi-MP3: finished with tracks left",
        "cancelled": "Multi-MP3: stopped",
    }.get(status, "Multi-MP3: run failed")
    if not total:
        return title, f"Run {status}."
    parts = [f"{done}/{total} tracks downloaded"]
    if failed:
        parts.append(f"{failed} failed")
    if pending:
        parts.append(f"{pending} still to do")
    return title, " · ".join(parts)
