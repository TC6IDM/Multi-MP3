"""Thread-safe, replayable event log bridging download threads to SSE streams.

asyncio.Queue is NOT thread-safe — calling put_nowait() from a worker thread
corrupts the event loop's internal future state and causes hangs/crashes.
This bus uses plain lists + a threading.Lock (safe from any thread).

It used to be a queue that the one SSE connection drained, so refreshing the
browser mid-run lost everything already sent — and closing the tab closed the
bus, so even reconnecting got nothing. Now every event gets a sequence number
and stays in the log; each reader keeps its own cursor:

  * a fresh page replays from 0 and rebuilds the whole Live tab exactly,
  * EventSource's automatic reconnect sends Last-Event-ID and resumes where
    it dropped, with no duplicates,
  * any number of tabs can watch the same run.

Two lanes:
  * control — lifecycle/state events (link.*, track.progress, job.*). Kept in
    full (up to a generous cap): replaying them is how the UI rebuilds state.
  * bulk    — log lines. Only the most recent few hundred are kept.
"""

from __future__ import annotations

import threading
from bisect import bisect_right

# Event types that carry state — losing one leaves the UI showing a stale
# state (e.g. a card stuck on "Downloading" forever).
_CONTROL_MARKERS = (
    '"type": "link.',
    '"type": "job.',
    '"type": "progress.',
    '"type": "task.',
    '"type": "provider.',         # rate-limiter cooldowns / rate changes
    '"type": "track.progress"',   # per-track state; "track.log" is sheddable
)


class _Lane:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.seqs: list[int] = []
        self.items: list[str] = []

    def append(self, seq: int, payload: str) -> int:
        self.seqs.append(seq)
        self.items.append(payload)
        # Trim in chunks rather than per event, so a long run isn't paying for
        # a list shift on every push.
        excess = len(self.items) - self.cap
        if excess > self.cap // 10:
            del self.seqs[:excess]
            del self.items[:excess]
            return excess
        return 0

    def after(self, seq: int) -> int:
        return bisect_right(self.seqs, seq)


class EventBus:
    """Bounded, thread-safe, replayable two-lane event log."""

    def __init__(self, maxlen: int = 60_000, log_maxlen: int = 600) -> None:
        self._control = _Lane(maxlen)
        self._bulk = _Lane(log_maxlen)
        self._lock = threading.Lock()
        self._seq = 0
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
            self._seq += 1
            lane = self._control if self._is_control(payload) else self._bulk
            self._dropped += lane.append(self._seq, payload)

    def read(self, after: int = 0, limit: int = 500) -> list[tuple[int, str]]:
        """Events with a sequence number above `after`, oldest first.

        Control and log events come back interleaved in the order they were
        pushed, so a replay is indistinguishable from having watched live.
        """
        with self._lock:
            c, b = self._control.after(after), self._bulk.after(after)
            cs, ci = self._control.seqs, self._control.items
            bs, bi = self._bulk.seqs, self._bulk.items
            out: list[tuple[int, str]] = []
            while len(out) < limit and (c < len(cs) or b < len(bs)):
                if b >= len(bs) or (c < len(cs) and cs[c] < bs[b]):
                    out.append((cs[c], ci[c]))
                    c += 1
                else:
                    out.append((bs[b], bi[b]))
                    b += 1
            return out

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def close(self) -> None:
        """No more events will be pushed. Reading still works (for replays)."""
        with self._lock:
            self._closed = True

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def __len__(self) -> int:
        with self._lock:
            return len(self._control.items) + len(self._bulk.items)
