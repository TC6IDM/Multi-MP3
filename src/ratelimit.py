"""An adaptive rate limiter shared by every worker of one provider.

SoundCloud rate-limits by IP, and once the wall is hit every further request
fails and keeps the block alive. The old design had no shared notion of a
budget: each scdl process paced itself, so four playlists in parallel made four
times the requests, and when the limit tripped scdl carried on failing every
remaining track. A 460-track playlist died at ~102 that way.

This limiter is one token bucket per provider, counting *track starts* per
minute across all workers and playlists in the run:

* additive increase — after each stretch of clean tracks the rate creeps up
  toward max_rate, so it finds the real edge rather than guessing it;
* multiplicative decrease — on a rate-limit response the rate halves and every
  worker pauses for a cooldown (Retry-After if the server sent one, otherwise
  60s, 180s, 600s on consecutive hits);
* memory — the rate it ends on is saved, and the next run starts just under it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

Event = Callable[[str, dict[str, Any]], None]


class CancelToken(Protocol):
    """Anything shaped like threading.Event (the engine combines two)."""

    def is_set(self) -> bool: ...
    def wait(self, timeout: float) -> bool: ...


def _default_wait(seconds: float, cancel: CancelToken | None) -> bool:
    """Sleep, waking early if cancelled. True means cancelled."""
    if cancel is not None:
        return cancel.wait(seconds)
    time.sleep(seconds)
    return False


class AdaptiveRateLimiter:
    def __init__(self, name: str, *, rate: float, min_rate: float, max_rate: float,
                 burst: float = 1.0, increase_every: int = 10, step: float = 1.0,
                 cooldowns: Sequence[float] = (60, 180, 600),
                 state_file: Path | None = None, use_learned: bool = True,
                 on_event: Event | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 wait: Callable[[float, CancelToken | None], bool] = _default_wait) -> None:
        self.name = name
        self._min = max(0.1, float(min_rate))
        self._max = max(self._min, float(max_rate))
        self._burst = max(1.0, float(burst))
        self._increase_every = max(1, int(increase_every))
        self._step = float(step)
        self._cooldowns = tuple(cooldowns) or (60,)
        self._state_file = state_file
        self._on_event = on_event
        self._clock = clock
        self._wait = wait
        self._lock = threading.Lock()

        start = float(rate)
        learned = self._load_learned() if use_learned else None
        if learned:
            # Just under where the last run settled — it may have been about to
            # trip again, and the limit drifts between runs.
            start = learned * 0.9
        self._rate = min(self._max, max(self._min, start))

        now = self._clock()
        self._tokens = self._burst
        self._last = now
        self._paused_until = 0.0
        self._pause_started = float("-inf")
        self._streak = 0
        self._hits = 0          # consecutive cooldowns without a clean stretch

    # ── Public API ──────────────────────────────────────────────────────

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    @property
    def consecutive_hits(self) -> int:
        with self._lock:
            return self._hits

    def acquire(self, cancel: CancelToken | None = None) -> bool:
        """Block until a track may start. False if cancelled while waiting."""
        while True:
            with self._lock:
                now = self._clock()
                if now < self._paused_until:
                    delay = self._paused_until - now
                else:
                    self._refill(now)
                    if self._tokens >= 1.0:
                        self._tokens -= 1.0
                        return True
                    delay = (1.0 - self._tokens) * 60.0 / self._rate
            if cancel is not None and cancel.is_set():
                return False
            # Slices keep Cancel responsive through a ten-minute cooldown.
            if self._wait(min(delay, 1.0), cancel):
                return False

    def on_success(self) -> None:
        changed = False
        with self._lock:
            self._streak += 1
            if self._streak >= self._increase_every:
                self._streak = 0
                self._hits = 0      # a clean stretch forgives earlier cooldowns
                if self._rate < self._max:
                    self._rate = min(self._max, self._rate + self._step)
                    changed = True
            rate = self._rate
        if changed:
            self._save_learned(rate)
            self._emit("rate", {"rate": round(rate, 1)})

    def on_rate_limited(self, retry_after: float | None = None,
                        issued_at: float | None = None, reason: str = "") -> bool:
        """Back off after a rate-limit response. True if this started a cooldown.

        Several workers usually hit the wall together. Only the first starts a
        cooldown; the rest — requests issued before that cooldown began — are
        the same event, not fresh evidence, and are ignored.
        """
        with self._lock:
            now = self._clock()
            if now < self._paused_until:
                return False
            if issued_at is not None and issued_at <= self._pause_started:
                return False
            self._hits += 1
            self._rate = max(self._min, self._rate / 2)
            if retry_after and retry_after > 0:
                cooldown = float(min(max(retry_after, 5.0), 3600.0))
            else:
                cooldown = float(self._cooldowns[min(self._hits - 1, len(self._cooldowns) - 1)])
            self._pause_started = now
            self._paused_until = now + cooldown
            # One track may start the moment the cooldown ends; nothing accrues
            # during the pause, so the rest follow at the new, halved rate.
            self._tokens = 1.0
            self._last = self._paused_until
            self._streak = 0
            rate, hits = self._rate, self._hits
        self._save_learned(rate)
        self._emit("cooldown", {"seconds": round(cooldown), "rate": round(rate, 1),
                                "hits": hits, "reason": reason[:200]})
        return True

    def stamp(self) -> float:
        """A timestamp to pass back as issued_at when reporting a failure."""
        return self._clock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            return {
                "name": self.name, "rate": round(self._rate, 1),
                "min_rate": self._min, "max_rate": self._max,
                "paused_for": round(max(0.0, self._paused_until - now), 1),
                "hits": self._hits,
            }

    # ── Internals ───────────────────────────────────────────────────────

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate / 60.0)
        self._last = now

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self._on_event:
            try:
                self._on_event(kind, data)
            except Exception:
                pass

    def _load_learned(self) -> float | None:
        if not self._state_file:
            return None
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            rate = float(data.get("rate") or 0)
            return rate if rate > 0 else None
        except (OSError, ValueError, AttributeError):
            return None

    def _save_learned(self, rate: float) -> None:
        if not self._state_file:
            return
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps({"rate": round(rate, 2), "updated_at": time.time()}),
                encoding="utf-8")
        except OSError:
            pass
