"""Thread-safe event bus bridging synchronous download threads to async SSE streaming.

asyncio.Queue is NOT thread-safe — calling put_nowait() from a worker thread
corrupts the event loop's internal future state and causes hangs/crashes.
This bus uses plain deques + a threading.Lock (safe from any thread) and is
drained in batches by the async SSE generator.

Two priority lanes:
  * control — lifecycle/state events (link.started, track.progress, job.*).
    Large cap; these drive the UI and must not be silently dropped.
  * bulk    — log lines. Small cap; oldest are evicted under flood, because
    stale log spam is worthless but a lost state transition wedges the UI.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List

# Event types that must survive backpressure — losing one leaves the UI
# showing a stale state (e.g. a card stuck on "Downloading" forever).
_CONTROL_MARKERS = (
    '"type": "link.',
    '"type": "job.',
    '"type": "progress.',
    '"type": "task.',
    '"type": "track.progress"',   # per-track state; "track.log" is sheddable
)


class EventBus:
    """Bounded, thread-safe, two-lane event buffer."""

    def __init__(self, maxlen: int = 2000, log_maxlen: int = 600) -> None:
        self._control: Deque[str] = deque(maxlen=maxlen)
        self._bulk: Deque[str] = deque(maxlen=log_maxlen)
        self._lock = threading.Lock()
        self._closed = False
        self._dropped = 0

    @staticmethod
    def _is_control(payload: str) -> bool:
        head = payload[:64]
        return any(m in head for m in _CONTROL_MARKERS)

    def push(self, payload: str) -> None:
        """Append an event. Safe to call from any thread."""
        with self._lock:
            if self._closed:
                return
            lane = self._control if self._is_control(payload) else self._bulk
            if len(lane) == lane.maxlen:
                self._dropped += 1  # deque evicts the oldest automatically
            lane.append(payload)

    def drain(self, limit: int = 200) -> List[str]:
        """Pop up to `limit` events, control lane first so state transitions
        are never starved by log volume. Called from the event loop thread."""
        out: List[str] = []
        with self._lock:
            while self._control and len(out) < limit:
                out.append(self._control.popleft())
            while self._bulk and len(out) < limit:
                out.append(self._bulk.popleft())
        return out

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._control) + len(self._bulk)
