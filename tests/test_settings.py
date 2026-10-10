from src.settings import lane


def test_balanced_defaults():
    sc = lane("soundcloud", "balanced")
    assert (sc.workers, sc.rate, sc.max_rate) == (3, 20, 40)


def test_env_overrides_tune_balanced(monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_WORKERS", "5")
    monkeypatch.setenv("SOUNDCLOUD_RATE", "25")
    sc = lane("soundcloud", "balanced")
    assert (sc.workers, sc.rate) == (5, 25)


def test_fast_is_derived_from_balanced(monkeypatch):
    monkeypatch.setenv("YOUTUBE_WORKERS", "4")
    yt = lane("youtube", "fast")
    assert yt.workers == 6
    assert yt.rate == 45 and yt.max_rate == 90


def test_careful_ignores_env_overrides(monkeypatch):
    # Careful is the fallback for when tuned settings are getting you blocked.
    monkeypatch.setenv("SOUNDCLOUD_WORKERS", "8")
    monkeypatch.setenv("SOUNDCLOUD_RATE", "50")
    sc = lane("soundcloud", "careful")
    assert (sc.workers, sc.rate) == (1, 8)


def test_max_workers_override_and_unknown_speed():
    assert lane("spotify", "balanced", max_workers=7).workers == 7
    assert lane("soundcloud", "warp-speed") == lane("soundcloud", "balanced")


def test_starting_rate_is_clamped_into_range(monkeypatch):
    monkeypatch.setenv("SOUNDCLOUD_RATE", "500")
    assert lane("soundcloud", "balanced").rate == 40
