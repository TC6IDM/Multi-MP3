import json

from src.event_bus import EventBus


def ev(kind, **kw):
    return json.dumps({"type": kind, **kw})


def test_replay_from_start_returns_everything_in_order():
    bus = EventBus()
    bus.push(ev("job.started"))
    bus.push(ev("log", message="hello"))
    bus.push(ev("track.progress", event="track_complete"))
    seqs = [s for s, _ in bus.read(0)]
    kinds = [json.loads(p)["type"] for _, p in bus.read(0)]
    assert seqs == [1, 2, 3]
    assert kinds == ["job.started", "log", "track.progress"]


def test_cursor_resumes_without_duplicates():
    bus = EventBus()
    for i in range(10):
        bus.push(ev("track.progress", i=i))
    first = bus.read(0, limit=4)
    rest = bus.read(first[-1][0])
    assert [json.loads(p)["i"] for _, p in first + rest] == list(range(10))


def test_two_readers_each_see_every_event():
    bus = EventBus()
    bus.push(ev("link.started"))
    assert bus.read(0) == bus.read(0)       # reading doesn't consume


def test_log_lines_are_capped_but_state_is_kept():
    bus = EventBus(maxlen=1000, log_maxlen=10)
    bus.push(ev("link.started"))
    for i in range(50):
        bus.push(ev("log", message=str(i)))
    events = [json.loads(p) for _, p in bus.read(0, limit=1000)]
    assert events[0]["type"] == "link.started"
    logs = [e for e in events if e["type"] == "log"]
    assert 10 <= len(logs) <= 11 and logs[-1]["message"] == "49"


def test_closed_bus_still_replays():
    bus = EventBus()
    bus.push(ev("job.completed"))
    bus.close()
    bus.push(ev("log", message="too late"))
    assert bus.closed and len(bus.read(0)) == 1 and bus.last_seq == 1
