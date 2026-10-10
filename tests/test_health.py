"""Spotting a yt-dlp that's fallen behind a site."""

from datetime import date

from src import health


def test_version_age_from_the_release_date():
    assert health.version_age_days("2026.8.19", date(2026, 10, 10)) == 52
    assert health.version_age_days("2026.08.19.232156", date(2026, 8, 20)) == 1
    assert health.version_age_days("nonsense") is None


def test_no_alert_for_a_normal_run(tmp_path):
    for _ in range(3):
        health.record(tmp_path, "youtube", 38, ["Video unavailable"])
    assert health.assess(tmp_path, "2026.8.19") == []


def test_alert_when_failures_jump_well_above_usual(tmp_path):
    for _ in range(3):
        health.record(tmp_path, "youtube", 39, ["x"])
    health.record(tmp_path, "youtube", 20, ["ERROR: Unable to extract nsig function"] * 20)
    [alert] = health.assess(tmp_path, "2026.1.1")
    assert alert["provider"] == "youtube" and alert["failed"] == 20 and alert["total"] == 40
    assert "usually 2%" in alert["message"]
    assert "no longer understanding the site" in alert["message"]
    assert "Update yt-dlp" in alert["message"]


def test_small_runs_say_nothing(tmp_path):
    health.record(tmp_path, "soundcloud", 1, ["boom", "boom"])
    assert health.assess(tmp_path, "2026.8.19") == []


def test_spotify_is_judged_on_spotdls_own_yt_dlp(tmp_path):
    health.record(tmp_path, "spotify", 5, ["AudioProviderError: YT-DLP download error"] * 5)
    [alert] = health.assess(tmp_path, ytdlp="2026.10.1", spotdl_ytdlp="2025.12.8")
    assert "2025.12.8" in alert["message"]
