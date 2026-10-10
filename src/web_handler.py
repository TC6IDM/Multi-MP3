"""Custom logging handler that pushes records to a thread-safe bus for SSE streaming."""

from __future__ import annotations

import json
import logging
import time

from src.event_bus import EventBus


class SSELogHandler(logging.Handler):
    """Pushes formatted log records into an EventBus for SSE streaming.

    Uses EventBus (deque + lock) rather than asyncio.Queue because log records
    are emitted from download worker threads, and asyncio.Queue is not
    thread-safe.
    """

    def __init__(self, event_bus: EventBus, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._bus = event_bus
        self.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(threadName)s | %(message)s'))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            payload = json.dumps({
                "type": "log",
                "ts": time.time(),
                "level": record.levelname,
                "message": msg,
            })
            self._bus.push(payload)
        except Exception:
            self.handleError(record)
