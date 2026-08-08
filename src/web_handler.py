"""Custom logging handler that pushes records to an async queue for SSE streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time


class SSELogHandler(logging.Handler):
    """Pushes formatted log records into an asyncio.Queue for SSE streaming."""

    def __init__(self, event_queue: asyncio.Queue, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self._queue = event_queue
        self.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            payload = json.dumps({
                "type": "log",
                "ts": time.time(),
                "level": record.levelname,
                "message": msg,
            })
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass
        except Exception:
            self.handleError(record)
