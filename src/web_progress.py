"""Web-backed DownloadProgress — pushes events to an async queue for SSE streaming."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from src.event_bus import EventBus
from src.tui import DownloadProgress


class WebProgress(DownloadProgress):
    """DownloadProgress that emits structured JSON events for web SSE streaming."""

    def __init__(self, event_bus: EventBus | None = None) -> None:
        super().__init__(enabled=False)  # Never use rich in web mode
        self._bus = event_bus
        # Lanes advance their tasks from several threads at once.
        self._task_counts: dict[str, dict[str, Any]] = {}
        self._task_lock = threading.Lock()

    def set_bus(self, bus: EventBus) -> None:
        self._bus = bus

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        """Push a structured event to the bus. Safe from any thread."""
        if self._bus is None:
            return
        try:
            payload = json.dumps({"type": event_type, "ts": time.time(), **data})
            self._bus.push(payload)
        except Exception:
            pass

    def add_task(self, name: str, total: int = 1) -> str:
        with self._task_lock:
            self._task_counts[name] = {"total": total, "completed": 0}
        self._emit("task.add", {"name": name, "total": total})
        return name

    def advance(self, name: str) -> None:
        with self._task_lock:
            task = self._task_counts.get(name)
            if task is None:
                return
            task["completed"] += 1
            completed, total = task["completed"], task["total"]
        self._emit("task.advance", {"name": name, "completed": completed, "total": total})

    def update_description(self, name: str, description: str) -> None:
        self._emit("task.update", {"name": name, "description": description})

    def start(self) -> None:
        self._emit("progress.start", {})

    def stop(self) -> None:
        self._emit("progress.stop", {})

    def link_started(self, provider: str, link: str, name: str, total_tracks: int = 0,
                     queued: bool = False) -> None:
        self._emit("link.started", {
            "provider": provider,
            "link": link,
            "name": name or link.split("?")[0].split("/")[-1] or link,
            "total_tracks": total_tracks,
            # Resolved but waiting for a worker: the card learns its name and
            # size without claiming to be downloading yet.
            "queued": queued,
        })

    def provider_status(self, provider: str, kind: str, data: dict[str, Any]) -> None:
        # "provider.cooldown" drives the Live tab's countdown banner;
        # "provider.rate" keeps its tracks/min readout current.
        self._emit(f"provider.{kind}", {"provider": provider, **data})

    def link_complete(self, provider: str, link: str, name: str, code: int) -> None:
        self._emit("link.complete", {
            "provider": provider,
            "link": link,
            "name": name,
            "code": code,
        })

    def track_progress(self, provider: str, link: str, event: str, data: dict[str, Any]) -> None:
        # Raw subprocess lines are disposable and high-volume, so they go out
        # under a separate type that the bus treats as sheddable. Everything
        # else here is real state (a track started/finished) and must not be
        # dropped under load, or the UI ends up stuck at e.g. 30/40.
        kind = "track.log" if event == "log" else "track.progress"
        self._emit(kind, {
            "provider": provider,
            "link": link,
            "event": event,
            **data,
        })

    def link_metadata(self, provider: str, link: str, tracks: list[dict[str, Any]]) -> None:
        self._emit("link.metadata", {
            "provider": provider,
            "link": link,
            "tracks": tracks,
        })
