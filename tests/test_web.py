import pytest
from fastapi.testclient import TestClient

import web_app
from src.job_manager import JobManager
from src.state import DONE, StateStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    out = tmp_path / "downloads"
    (out / "My Playlist").mkdir(parents=True)
    (out / "My Playlist" / "01 A - Song.mp3").write_bytes(b"ID3fake")
    # A secret just outside the output dir, like the real .env.
    (tmp_path / ".env").write_text("CLIENTSECRET=hunter2", encoding="utf-8")
    monkeypatch.setattr(web_app, "DEFAULT_OUTPUT", out.resolve())
    monkeypatch.setattr(web_app, "DEFAULT_LINKS", tmp_path / "links.txt")
    # Jobs are created but never actually run in these tests.
    monkeypatch.setattr(web_app, "_launch", lambda job: None)
    return TestClient(web_app.app)


@pytest.mark.parametrize("url", [
    "/api/files/..%2F.env/download",
    "/api/files/%2e%2e/%2e%2e/.env/download",
    "/api/files/My%20Playlist/..%2F..%2F.env/download",
])
def test_download_cannot_escape_the_output_dir(client, url):
    r = client.get(url)
    assert r.status_code == 404
    assert "hunter2" not in r.text


def test_file_listing_cannot_escape_the_output_dir(client):
    assert client.get("/api/files", params={"path": ".."}).status_code == 404


def test_playlist_page_cannot_escape_the_output_dir(client):
    assert client.get("/playlist/..%2F..").status_code == 404


def test_playlist_page_renders(client):
    # Was a TypeError on every request: render_template's own `name` parameter
    # collided with the page's `name=` context variable.
    r = client.get("/playlist/My%20Playlist")
    assert r.status_code == 200
    assert "01 A - Song" in r.text
    assert "/api/files/My%20Playlist/01%20A%20-%20Song.mp3/download" in r.text


def test_legitimate_download_still_works(client):
    r = client.get("/api/files/My%20Playlist/01%20A%20-%20Song.mp3/download")
    assert r.status_code == 200
    assert r.content == b"ID3fake"


def test_non_ascii_filenames_download(client):
    # The old hand-built Content-Disposition header couldn't carry "å".
    path = web_app.DEFAULT_OUTPUT / "My Playlist" / "02 Måneskin - Zitti.mp3"
    path.write_bytes(b"x")
    r = client.get("/api/files/My%20Playlist/02%20M%C3%A5neskin%20-%20Zitti.mp3/download")
    assert r.status_code == 200


def test_state_listing_and_reset(client):
    store = StateStore(web_app.DEFAULT_OUTPUT)
    st = store.open("soundcloud", "https://soundcloud.com/a/sets/b")
    st.update_meta(name="B", folder="My Playlist")
    st.sync_tracks([{"key": "1", "index": 1, "title": "x"}, {"key": "2", "index": 2, "title": "y"}])
    st.mark("1", DONE)

    [row] = client.get("/api/state").json()
    assert (row["done"], row["left"], row["has_folder"]) == (1, 1, True)

    detail = client.get(f"/api/state/soundcloud/{row['key']}").json()
    assert [t["key"] for t in detail["tracks"]] == ["1", "2"]

    r = client.post("/api/state/reset",
                    json={"targets": [{"provider": "soundcloud", "key": row["key"]}]})
    assert r.json() == {"reset": 1}
    assert client.get("/api/state").json()[0]["done"] == 0


def test_reset_is_refused_while_a_job_runs(client):
    mgr = JobManager()
    job = mgr.create_job(["https://soundcloud.com/a/sets/b"], ["soundcloud"])
    try:
        assert client.post("/api/state/reset", json={}).status_code == 409
    finally:
        mgr.complete_job(0)
    assert mgr.get_job(job.job_id).status == "completed"


def test_status_reports_tooling(client):
    s = client.get("/api/status").json()
    assert {"spotdl", "deno", "speeds", "queue"} <= set(s)
    assert s["speeds"] == ["careful", "balanced", "fast"]


# ── Job queue + replayable events ─────────────────────────────────────

def test_second_job_is_queued_instead_of_refused(client):
    body = {"links": "https://soundcloud.com/a/sets/b", "providers": ["soundcloud"]}
    first = client.post("/api/jobs", json=body).json()
    second = client.post("/api/jobs", json=body).json()
    assert (first["status"], second["status"], second["position"]) == ("running", "queued", 1)
    assert [q["job_id"] for q in client.get("/api/status").json()["queue"]] == [second["job_id"]]
    assert client.delete(f"/api/jobs/{second['job_id']}").status_code == 200
    assert client.get("/api/status").json()["queue"] == []


def _bus_with(job_id, n):
    import json as _json

    from src.event_bus import EventBus
    bus = EventBus()
    for i in range(n):
        bus.push(_json.dumps({"type": "track.progress", "i": i}))
    bus.close()
    web_app._buses()[job_id] = bus
    return bus


def _sse_data(text):
    import json as _json
    return [_json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def test_events_replay_from_the_start_for_a_fresh_page(client):
    _bus_with("replayjob", 3)
    events = _sse_data(client.get("/api/jobs/replayjob/events").text)
    assert [e.get("i") for e in events[:3]] == [0, 1, 2]
    assert events[-1]["type"] == "done"


def test_events_resume_after_last_event_id(client):
    _bus_with("resumejob", 5)
    r = client.get("/api/jobs/resumejob/events", headers={"Last-Event-ID": "3"})
    events = _sse_data(r.text)
    assert [e.get("i") for e in events if "i" in e] == [3, 4]
    assert "id: 4" in r.text


# ── Link manager ──────────────────────────────────────────────────────

def test_links_entries_and_structured_save(client):
    web_app.DEFAULT_LINKS.write_text(
        "# mixes\nhttps://soundcloud.com/a/sets/b old label\n#https://open.spotify.com/playlist/X\n",
        encoding="utf-8")
    data = client.get("/api/links").json()
    assert [(e["provider"], e["enabled"], e["label"]) for e in data["entries"]] == [
        ("soundcloud", True, "old label"), ("spotify", False, "")]
    entries = data["entries"]
    entries[0]["label"] = "new label"
    entries[1]["enabled"] = True
    saved = client.put("/api/links", json={"entries": entries}).json()
    assert saved["text"] == ("# mixes\nhttps://soundcloud.com/a/sets/b new label\n"
                             "https://open.spotify.com/playlist/X\n")
    assert saved["parsed"]["total"] == 2


# ── Library, covers, failed-tracks report ─────────────────────────────

def _record(client, statuses):
    store = StateStore(web_app.DEFAULT_OUTPUT)
    st = store.open("soundcloud", "https://soundcloud.com/a/sets/b")
    st.update_meta(name="My Playlist", folder="My Playlist")
    st.sync_tracks([{"key": str(i), "index": i, "title": f"T{i}", "artists": ["Art"]}
                    for i in range(1, len(statuses) + 1)])
    for i, status in enumerate(statuses, 1):
        st.mark(str(i), status, error="HTTP Error 404" if status != DONE else None)
    return st


def test_library_merges_records_with_folders_and_sizes(client):
    _record(client, [DONE, "failed"])
    (web_app.DEFAULT_OUTPUT / "Old Download").mkdir()
    (web_app.DEFAULT_OUTPUT / ".icons").mkdir()
    (web_app.DEFAULT_OUTPUT / ".icons" / "My Playlist.jpg").write_bytes(b"jpg")
    lib = client.get("/api/library").json()
    by_name = {i["name"]: i for i in lib["items"]}
    assert by_name["My Playlist"]["done"] == 1 and by_name["My Playlist"]["files"] == 1
    assert by_name["My Playlist"]["cover"] == "/api/covers/My%20Playlist.jpg"
    assert by_name["Old Download"]["key"] is None
    assert client.get(by_name["My Playlist"]["cover"]).content == b"jpg"
    assert client.get("/api/covers/..%2F..%2F.env").status_code == 404


def test_library_keeps_removed_songs_out_of_the_playlists(client):
    removed = web_app.DEFAULT_OUTPUT / "_removed" / "My Playlist"
    removed.mkdir(parents=True)
    (removed / "(0002) Gone.mp3").write_bytes(b"x" * 10)
    lib = client.get("/api/library").json()
    assert "_removed" not in {i["name"] for i in lib["items"]}
    assert (lib["removed_files"], lib["removed_bytes"]) == (1, 10)


def test_system_reports_health_and_levelling(client, monkeypatch):
    from src import health, maintenance
    monkeypatch.setattr(maintenance, "versions", lambda: {"yt_dlp": "2026.1.1", "spotdl_yt_dlp": None,
                                                          "ffmpeg": True})
    for _ in range(3):
        health.record(web_app.DEFAULT_OUTPUT, "youtube", 40, [])
    health.record(web_app.DEFAULT_OUTPUT, "youtube", 10, ["Unable to extract"] * 10)
    info = client.get("/api/system").json()
    assert [h["provider"] for h in info["health"]] == ["youtube"]
    assert info["level"]["enabled"] is True


def test_failed_report_and_retry(client):
    _record(client, [DONE, "failed", "unavailable"])
    [group] = client.get("/api/report/failed").json()
    assert [t["status"] for t in group["tracks"]] == ["failed", "unavailable"]
    assert group["tracks"][0]["query"] == "Art T2"
    text = client.get("/api/report/failed.txt").text
    assert "https://www.youtube.com/results?search_query=Art+T2" in text

    r = client.post("/api/state/retry", json={"targets": [{"provider": "soundcloud", "key": group["key"]}]})
    assert r.json()["tracks"] == 2
    assert client.get("/api/report/failed").json() == []


# ── Optional login ────────────────────────────────────────────────────

def test_basic_auth_middleware():
    import base64

    from fastapi import FastAPI

    inner = FastAPI()

    @inner.get("/x")
    def x():
        return {"ok": True}

    inner.add_middleware(web_app.BasicAuth, username="me", password="pw")
    c = TestClient(inner)
    assert c.get("/x").status_code == 401
    bad = base64.b64encode(b"me:nope").decode()
    assert c.get("/x", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"me:pw").decode()
    assert c.get("/x", headers={"Authorization": f"Basic {good}"}).json() == {"ok": True}


# ── Notifications ─────────────────────────────────────────────────────

def test_notify_formats_discord_and_ntfy(monkeypatch):
    import json as _json

    from src import notify as notify_mod

    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", lambda req, timeout: sent.append(req) or Resp())
    assert notify_mod.notify("T", "body", url="https://discord.com/api/webhooks/1/abc")
    assert _json.loads(sent[-1].data)["content"] == "**T**\nbody"
    assert notify_mod.notify("Multi-MP3: done", "3/3", url="https://ntfy.sh/topic")
    assert sent[-1].data == b"3/3" and sent[-1].get_header("Title") == "Multi-MP3: done"
    assert notify_mod.notify("T", "b", url="") is False
