"""Web-backed DownloadProgress — pushes events to an async queue for SSE streaming."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List

from src.tui import DownloadProgress


class WebProgress(DownloadProgress):
    """DownloadProgress that emits structured JSON events for web SSE streaming."""

    def __init__(self, event_queue: asyncio.Queue | None = None) -> None:
        super().__init__(enabled=False)  # Never use rich in web mode
        self._queue = event_queue
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def set_queue(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Push a structured event to the SSE queue."""
        if self._queue:
            try:
                payload = json.dumps({"type": event_type, "ts": time.time(), **data})
                self._queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def add_task(self, name: str, total: int = 1) -> str:
        self._tasks[name] = {"total": total, "completed": 0}
        self._emit("task.add", {"name": name, "total": total})
        return name

    def advance(self, name: str) -> None:
        if name in self._tasks:
            self._tasks[name]["completed"] += 1
            self._emit("task.advance", {
                "name": name,
                "completed": self._tasks[name]["completed"],
                "total": self._tasks[name]["total"],
            })

    def update_description(self, name: str, description: str) -> None:
        self._emit("task.update", {"name": name, "description": description})

    def start(self) -> None:
        self._emit("progress.start", {})

    def stop(self) -> None:
        self._emit("progress.stop", {})
