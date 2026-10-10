import threading

import pytest

from src.ratelimit import AdaptiveRateLimiter


class FakeClock:
    """A clock that only moves when the limiter waits on it."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def wait(self, seconds: float, cancel: threading.Event | None) -> bool:
        if cancel is not None and cancel.is_set():
            return True
        self.t += seconds
        return False


def make(clock, **kw):
    params = dict(rate=60, min_rate=4, max_rate=120, clock=clock, wait=clock.wait)
    params.update(kw)
    return AdaptiveRateLimiter("test", **params)


def test_acquire_paces_to_the_configured_rate():
    clock = FakeClock()
    lim = make(clock, rate=60)          # one track a second
    start = clock.t
    for _ in range(10):
        assert lim.acquire()
    # The first token is free (full bucket); the other nine cost a second each.
    assert clock.t - start == pytest.approx(9.0, abs=0.05)


def test_rate_limit_halves_rate_and_pauses_every_worker():
    clock = FakeClock()
    lim = make(clock, rate=40)
    lim.acquire()
    assert lim.on_rate_limited()
    assert lim.rate == 20
    before = clock.t
    lim.acquire()
    assert clock.t - before >= 60          # first cooldown in the schedule


def test_retry_after_header_sets_the_cooldown():
    clock = FakeClock()
    lim = make(clock)
    lim.on_rate_limited(retry_after=12)
    before = clock.t
    lim.acquire()
    assert 12 <= clock.t - before < 13


def test_consecutive_hits_escalate_the_cooldown():
    clock = FakeClock()
    lim = make(clock, cooldowns=(60, 180, 600))
    lim.on_rate_limited()
    lim.acquire()                           # waits out the first cooldown
    issued = lim.stamp()
    assert lim.on_rate_limited(issued_at=issued)
    before = clock.t
    lim.acquire()
    assert clock.t - before >= 180
    assert lim.consecutive_hits == 2


def test_hits_during_a_cooldown_count_once():
    clock = FakeClock()
    lim = make(clock)
    issued = lim.stamp()
    assert lim.on_rate_limited(issued_at=issued)
    # Other workers report the same wall a moment later.
    assert not lim.on_rate_limited(issued_at=issued)
    assert lim.rate == 30                   # halved once, not twice


def test_failures_from_requests_issued_before_the_cooldown_are_ignored():
    clock = FakeClock()
    lim = make(clock)
    old_request = lim.stamp()
    lim.on_rate_limited()
    lim.acquire()                           # cooldown over
    # A straggler from before the pause reports in late.
    assert not lim.on_rate_limited(issued_at=old_request)
    assert lim.consecutive_hits == 1


def test_clean_stretch_raises_the_rate_and_forgives_hits():
    clock = FakeClock()
    lim = make(clock, rate=20, increase_every=5, step=2)
    lim.on_rate_limited()
    assert lim.rate == 10
    for _ in range(5):
        lim.on_success()
    assert lim.rate == 12
    assert lim.consecutive_hits == 0


def test_rate_never_drops_below_min_or_exceeds_max():
    clock = FakeClock()
    lim = make(clock, rate=8, min_rate=5, max_rate=9, increase_every=1, cooldowns=(0.1,))
    for _ in range(5):
        lim.on_rate_limited()
        lim.acquire()
    assert lim.rate == 5
    for _ in range(20):
        lim.on_success()
    assert lim.rate == 9


def test_cancel_releases_a_blocked_acquire():
    clock = FakeClock()
    lim = make(clock)
    lim.on_rate_limited()                   # 60s cooldown ahead
    cancel = threading.Event()
    cancel.set()
    assert lim.acquire(cancel) is False


def test_learned_rate_is_saved_and_next_run_starts_just_under_it(tmp_path):
    state = tmp_path / "ratelimit.json"
    clock = FakeClock()
    lim = make(clock, rate=20, increase_every=1, step=10, state_file=state)
    lim.on_success()                        # 20 → 30, persisted
    assert state.is_file()

    again = make(FakeClock(), rate=20, state_file=state)
    assert again.rate == pytest.approx(27.0)

    explicit = make(FakeClock(), rate=20, state_file=state, use_learned=False)
    assert explicit.rate == 20


def test_cooldown_event_is_reported():
    clock = FakeClock()
    events = []
    lim = make(clock, on_event=lambda kind, data: events.append((kind, data)))
    lim.on_rate_limited(reason="HTTP Error 429")
    assert events and events[0][0] == "cooldown"
    assert events[0][1]["seconds"] == 60 and events[0][1]["rate"] == 30
