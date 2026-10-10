"""Multi-MP3 Web UI — FastAPI app with REST API + SSE + Jinja2 templates."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from src import health, loudness, maintenance
from src import links as links_file
from src.coordinator import Coordinator
from src.event_bus import EventBus
from src.job_manager import Job, JobManager
from src.notify import notify, run_summary
from src.settings import DEFAULT_SPEED, SPEEDS
from src.state import FAILED, PENDING, UNAVAILABLE, StateStore
from src.tidy import REMOVED_DIR
from src.utils import clean_url, deno_path, get_spotify_creds, provider_for, spotdl_command
from src.web_handler import SSELogHandler
from src.web_progress import WebProgress

load_dotenv()

DEFAULT_OUTPUT = Path(os.getenv("OUTPUT_DIR", "downloads")).resolve()
DEFAULT_LINKS = Path(os.getenv("LINKS_FILE", "links.txt"))
PROVIDERS = ("spotify", "youtube", "soundcloud")
# How long a finished job's event log stays around for a late (re)connect.
BUS_GRACE_SECONDS = 15 * 60


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    # Housekeeping that's always safe at startup: no job can be running yet.
    maintenance.cleanup(DEFAULT_OUTPUT)
    yield


app = FastAPI(title="Multi-MP3", version="2.1.0", lifespan=lifespan)


class BasicAuth:
    """Optional login (WEB_USERNAME + WEB_PASSWORD).

    The server listens on every interface, so anyone on the network can reach
    it. A plain ASGI middleware rather than @app.middleware, which buffers
    streaming responses and would stall the live event stream.
    """

    def __init__(self, app: Any, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    def _ok(self, header: str) -> bool:
        if not header.lower().startswith("basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        return secrets.compare_digest(user, self.username) and secrets.compare_digest(pw, self.password)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if self._ok(headers.get("authorization", "")):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"www-authenticate", b'Basic realm="Multi-MP3", charset="UTF-8"'),
            (b"content-type", b"text/plain; charset=utf-8")]})
        await send({"type": "http.response.body", "body": b"Login required"})


if os.getenv("WEB_USERNAME") and os.getenv("WEB_PASSWORD"):
    app.add_middleware(BasicAuth, username=os.environ["WEB_USERNAME"], password=os.environ["WEB_PASSWORD"])

# ── Static files & templates ─────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
templates_dir = Path(__file__).parent / "templates"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Helpers ──────────────────────────────────────────────────────────

def _get_jinja2():
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        return Environment(loader=FileSystemLoader(str(templates_dir)),
                           autoescape=select_autoescape(["html"]))
    except ImportError:
        return None


def render_template(template: str, **kwargs: Any) -> HTMLResponse:
    # The parameter was called `name`, which collided with the playlist page's
    # own `name=` context variable — every playlist page was a TypeError.
    env = _get_jinja2()
    if env is None:
        return HTMLResponse("<h1>Jinja2 not installed</h1>", status_code=500)
    tmpl = env.get_template(template)
    return HTMLResponse(tmpl.render(**kwargs))


def _safe_path(rel: str, root: Path | None = None) -> Path:
    """Resolve a user-supplied path inside the output directory, or 404.

    These paths come straight from the URL. Joining them onto the output dir
    without this check let `..` walk out of it — `/api/files/../.env/download`
    served the Spotify secret to anyone on the network, since the server binds
    0.0.0.0.
    """
    base = (root or DEFAULT_OUTPUT).resolve()
    try:
        path = (base / rel).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Not found")
    if path != base and not path.is_relative_to(base):
        raise HTTPException(status_code=404, detail="Not found")
    return path


def parse_link_text(text: str) -> list[str]:
    """Parse links from plain text input."""
    links: list[str] = []
    for line in text.splitlines():
        url = clean_url(line)
        if url:
            links.append(url)
    return links


def parse_link_counts(text: str) -> dict[str, int]:
    """How many links of each provider the text holds, plus the total.

    Shared by the saved-links response and the dashboard's live preview so the
    preview can never disagree with what starting a job would actually pick up.
    """
    counts = {"spotify": 0, "youtube": 0, "soundcloud": 0}
    for url in parse_link_text(text):
        provider = provider_for(url)
        if provider in counts:
            counts[provider] += 1
    return {**counts, "total": sum(counts.values())}


def _group_links(links: list[str], providers: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {p: [] for p in PROVIDERS}
    for link in links:
        provider = provider_for(link)
        if provider in grouped and provider in providers:
            grouped[provider].append(link)
    return grouped


def _buses() -> dict[str, EventBus]:
    buses: dict[str, EventBus] | None = getattr(app.state, "buses", None)
    if buses is None:
        buses = {}
        app.state.buses = buses
    return buses


def _cover_for(folder: str, name: str) -> str | None:
    """URL of a playlist's cover image, if one was saved."""
    icons = DEFAULT_OUTPUT / ".icons"
    stems = [Path(folder).name if folder else "", name,
             # Older Spotify runs saved covers under an alphanumeric-only name.
             "".join(c for c in name if c.isalnum() or c in (" ", "-", "_")).rstrip()]
    for stem in stems:
        if not stem:
            continue
        for ext in (".jpg", ".png", ".webp"):
            if (icons / f"{stem}{ext}").is_file():
                return f"/api/covers/{quote(stem + ext)}"
    return None


# ── Running jobs ─────────────────────────────────────────────────────

def _launch(job: Job) -> None:
    """Start a job's thread with a fresh event log."""
    bus = EventBus()
    _buses()[job.job_id] = bus
    threading.Thread(target=run_download_job, args=(job, DEFAULT_OUTPUT, bus),
                     daemon=True, name=f"job-{job.job_id}").start()


def run_download_job(job: Job, output_dir: Path, bus: EventBus) -> None:
    """Background thread: run the coordinator, then hand over to the next queued job."""
    mgr = JobManager()
    job_id = job.job_id

    logger = logging.getLogger(f"job_{job_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # Avoid duplicate handlers on repeat runs
    logger.propagate = False
    logger.addHandler(SSELogHandler(bus))
    logger.addHandler(logging.StreamHandler())

    # The link list rides on the first event so a page that connects (or
    # reconnects) later can lay out every playlist before replaying progress.
    bus.push(json.dumps({"type": "job.started", "job_id": job_id, "speed": job.speed,
                         "links": _group_links(job.links, job.providers)}))

    input_file = output_dir / ".web" / f"input_{job_id}.txt"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_text("\n".join(job.links), encoding="utf-8")

    client_id, client_secret = get_spotify_creds(logger)
    coord = Coordinator(output_dir, logger, client_id, client_secret,
                        speed=job.speed, progress=WebProgress(bus), cancel_event=mgr.cancel_event)
    try:
        exit_code = coord.process_all(input_file, job.providers)
    except Exception as e:
        logger.error(f"Job crashed: {e}")
        exit_code = 1
    finally:
        input_file.unlink(missing_ok=True)

    counts = coord.totals()
    finished = mgr.complete_job(exit_code, counts)
    status = finished.status if finished else ("completed" if exit_code == 0 else "failed")
    title, message = run_summary(status, counts)
    # "Update yt-dlp"-type suggestions ride along to the phone and the Live tab.
    notify(title, "\n".join([message, *coord.advice]), logger)
    bus.push(json.dumps({
        "type": "job.completed" if status == "completed" else f"job.{status}",
        "job_id": job_id, "exit_code": exit_code, "status": status,
        "summary": message, "title": title, "advice": coord.advice,
    }))
    bus.close()
    timer = threading.Timer(BUS_GRACE_SECONDS, lambda: _buses().pop(job_id, None))
    timer.daemon = True
    timer.start()

    nxt = mgr.start_next()
    if nxt is not None:
        _launch(nxt)


# ── Pages ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return render_template("index.html")


@app.get("/playlist/{name:path}", response_class=HTMLResponse)
def playlist_page(name: str):
    playlist_dir = _safe_path(name)
    tracks: list[dict[str, Any]] = []
    if playlist_dir.is_dir():
        for f in sorted(playlist_dir.iterdir()):
            if f.suffix.lower() == ".mp3":
                tracks.append({
                    "name": f.stem,
                    "path": f.relative_to(DEFAULT_OUTPUT).as_posix(),
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                })
    total_mb = round(sum(t["size_mb"] for t in tracks), 1)
    return render_template("playlist.html", name=name, tracks=tracks, total_mb=total_mb,
                           cover=_cover_for(name, name))


# ── Status & links ───────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    mgr = JobManager()
    job = mgr.get_job(mgr.current_job_id) if mgr.current_job_id else None
    return {
        "running": mgr.is_running(),
        "current_job_id": mgr.current_job_id,
        "current_status": job.status if job else None,
        "queue": [{"job_id": j.job_id, "links": len(j.links), "providers": j.providers,
                   "speed": j.speed, "created_at": j.created_at} for j in mgr.queued()],
        "spotdl": bool(spotdl_command()),
        "deno": bool(deno_path()),
        "providers": list(PROVIDERS),
        "speeds": list(SPEEDS),
        "default_speed": DEFAULT_SPEED,
        "output_dir": str(DEFAULT_OUTPUT),
        "notify_configured": bool(os.getenv("NOTIFY_URL", "").strip()),
    }


def _link_entries(text: str) -> list[dict[str, Any]]:
    """The Dashboard's link list, each with its playlist's sync state."""
    store = StateStore(DEFAULT_OUTPUT)
    out = []
    for e in links_file.entries(text):
        st = store.load(e.provider, e.key)
        s = st.summary() if st is not None else None
        out.append({**e.to_dict(), "record": None if s is None else {
            "name": s["name"], "total": s["total"], "done": s["done"], "left": s["left"],
            "failed": s["failed"], "unavailable": s["unavailable"]}})
    return out


@app.get("/api/links")
def api_get_links():
    text = DEFAULT_LINKS.read_text(encoding="utf-8") if DEFAULT_LINKS.is_file() else ""
    return {"text": text, "parsed": parse_link_counts(text), "entries": _link_entries(text)}


@app.post("/api/links/parse")
async def api_parse_links(data: dict[str, str]):
    """Count links in text the dashboard hasn't saved yet.

    The preview re-counts as the box is typed in, so it works on the posted
    text rather than the file on disk.
    """
    return {"parsed": parse_link_counts(data.get("text", ""))}


@app.put("/api/links")
def api_put_links(data: dict[str, Any]):
    """Save links.txt — raw text, or the Dashboard's list of entries."""
    if "entries" in data:
        current = DEFAULT_LINKS.read_text(encoding="utf-8") if DEFAULT_LINKS.is_file() else ""
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise HTTPException(status_code=400, detail="entries must be a list")
        text = links_file.apply_edits(current, entries)
    else:
        text = str(data.get("text", ""))
    DEFAULT_LINKS.write_text(text, encoding="utf-8")
    return {"saved": True, "text": text, "parsed": parse_link_counts(text),
            "entries": _link_entries(text)}


# ── Jobs ─────────────────────────────────────────────────────────────

@app.post("/api/jobs")
async def api_create_job(data: dict[str, Any]):
    mgr = JobManager()
    links_text = data.get("links", "")
    links = parse_link_text(links_text) if links_text else parse_link_text(
        DEFAULT_LINKS.read_text(encoding="utf-8") if DEFAULT_LINKS.is_file() else ""
    )
    if not links:
        raise HTTPException(status_code=400, detail="No valid links provided")

    providers = [p for p in data.get("providers", list(PROVIDERS)) if p in PROVIDERS]
    speed = data.get("speed")
    if speed not in SPEEDS:
        # Older clients sent a "parallel" flag; off meant one-at-a-time.
        speed = "careful" if data.get("parallel") is False else DEFAULT_SPEED

    # Unticking a provider drops its links here rather than only downstream:
    # the coordinator already skipped them, but they still reached the job and
    # the response, so the Live tab rendered cards that sat "Queued" forever.
    grouped = _group_links(links, providers)
    links = [link for bucket in grouped.values() for link in bucket]
    if not links:
        raise HTTPException(
            status_code=400,
            detail=f"No links for the selected provider(s): {', '.join(providers) or 'none'}",
        )

    mgr.set_history_path(DEFAULT_OUTPUT / ".web" / "jobs.json")
    job = mgr.create_job(links, providers, speed)
    if job.status == "running":
        _launch(job)
    return {
        "job_id": job.job_id,
        "status": job.status,
        "position": mgr.queue_position(job.job_id),
        "speed": speed,
        "links": grouped,
        "total_links": len(links),
    }


@app.get("/api/jobs")
async def api_list_jobs():
    mgr = JobManager()
    mgr.set_history_path(DEFAULT_OUTPUT / ".web" / "jobs.json")
    return mgr.get_jobs()


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = JobManager().get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
async def api_cancel_job(job_id: str):
    """Stop the running job, or take a queued one off the queue."""
    if not JobManager().cancel_job(job_id):
        raise HTTPException(status_code=400, detail="That job isn't running or queued")
    return {"cancelling": True}


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str, request: Request) -> StreamingResponse:
    """SSE stream of a job's events, replayable from any point.

    A fresh connection replays everything from the start, so a page that was
    refreshed mid-run rebuilds the Live tab exactly. Every event carries an
    `id:`, and EventSource's automatic reconnect sends the last one back as
    Last-Event-ID, so a dropped connection resumes without duplicates.
    """
    bus = _buses().get(job_id)
    mgr = JobManager()
    if bus is None:
        job = mgr.get_job(job_id)
        status = job.status if job else "unknown"

        async def gone() -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'type': 'done', 'status': status, 'reason': 'no stream'})}\n\n"
        return StreamingResponse(gone(), media_type="text/event-stream")

    try:
        cursor = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
    except ValueError:
        cursor = 0

    async def event_stream() -> AsyncGenerator[str, None]:
        nonlocal cursor
        idle_ticks = 0
        tick = 0.2          # seconds between reads when caught up
        ping_after = 75     # ~15s of idle ticks before a keepalive
        while True:
            batch = bus.read(cursor, 500)
            if batch:
                idle_ticks = 0
                cursor = batch[-1][0]
                # Coalesce the batch into a single network write
                yield "".join(f"id: {seq}\ndata: {payload}\n\n" for seq, payload in batch)
                await asyncio.sleep(0)       # a long replay mustn't hog the loop
                continue
            if bus.closed and cursor >= bus.last_seq:
                job = mgr.get_job(job_id)
                status = job.status if job else "completed"
                yield f"data: {json.dumps({'type': 'done', 'status': status})}\n\n"
                break
            if await request.is_disconnected():
                break
            idle_ticks += 1
            if idle_ticks >= ping_after:
                idle_ticks = 0
                yield ": ping\n\n"
            await asyncio.sleep(tick)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ── Progress records ─────────────────────────────────────────────────

def _refuse_while_running() -> None:
    # Records are written by the running job from its own in-memory copy, so
    # an edit made here meanwhile would be silently overwritten.
    if JobManager().is_running():
        raise HTTPException(status_code=409, detail="Wait for the current download to finish")


def _targets(data: dict[str, Any] | None) -> list[tuple[str, str]] | None:
    targets = (data or {}).get("targets")
    if targets is None:
        return None
    if not isinstance(targets, list):
        raise HTTPException(status_code=400, detail="targets must be a list")
    return [(str(t.get("provider", "")), str(t.get("key", ""))) for t in targets if isinstance(t, dict)]


@app.get("/api/state")
def api_state():
    """Every playlist's progress record, summarised."""
    items = []
    for st in StateStore(DEFAULT_OUTPUT).all():
        s = st.summary()
        folder = s.get("folder") or ""
        s["has_folder"] = bool(folder) and (DEFAULT_OUTPUT / folder).is_dir()
        items.append(s)
    items.sort(key=lambda s: (s.get("name") or "").lower())
    return items


@app.get("/api/state/{provider}/{key}")
def api_state_detail(provider: str, key: str):
    st = StateStore(DEFAULT_OUTPUT).load(provider, key)
    if st is None:
        raise HTTPException(status_code=404, detail="No record for that playlist")
    return {**st.summary(), "tracks": st.ordered()}


@app.post("/api/state/reset")
def api_state_reset(data: dict[str, Any] | None = None):
    """Forget saved progress so the next run re-downloads (and overwrites).

    Body: {"targets": [{"provider": "...", "key": "..."}]} for specific
    playlists, or no targets for every playlist.
    """
    _refuse_while_running()
    return {"reset": StateStore(DEFAULT_OUTPUT).reset(_targets(data))}


@app.post("/api/state/retry")
def api_state_retry(data: dict[str, Any] | None = None):
    """Give failed and unavailable tracks another go on the next run.

    A normal run already retries failures; this also brings back tracks that
    were marked unavailable (removed, private, region-locked), which runs
    otherwise skip so they don't cost a request every time.
    """
    _refuse_while_running()
    store = StateStore(DEFAULT_OUTPUT)
    targets = _targets(data)
    states = store.all() if targets is None else [s for s in (store.load(p, k) for p, k in targets) if s]
    count = 0
    for st in states:
        for t in st.ordered():
            if t.get("status") in (FAILED, UNAVAILABLE):
                st.mark(t["key"], PENDING, error="", save=False)
                count += 1
        st.save()
    return {"tracks": count, "playlists": len(states)}


def _failed_report() -> list[dict[str, Any]]:
    report = []
    for st in StateStore(DEFAULT_OUTPUT).all():
        bad = [t for t in st.ordered() if t.get("status") in (FAILED, UNAVAILABLE)]
        if not bad:
            continue
        s = st.summary()
        report.append({
            "provider": s["provider"], "key": s["key"], "name": s["name"], "url": s["url"],
            "tracks": [{
                "index": t.get("index"), "title": t.get("title") or f"Track {t.get('index')}",
                "artists": t.get("artists") or [], "status": t.get("status"),
                "error": t.get("error") or "",
                "query": " ".join([*(t.get("artists") or [])[:1], t.get("title") or ""]).strip(),
            } for t in bad],
        })
    report.sort(key=lambda r: r["name"].lower())
    return report


@app.get("/api/report/failed")
def api_failed_report():
    """Every failed or unavailable track, grouped by playlist."""
    return _failed_report()


@app.get("/api/report/failed.txt", response_class=PlainTextResponse)
def api_failed_report_text():
    """The same report as text, with search links for finding each track by hand."""
    lines = ["Multi-MP3 — tracks that couldn't be downloaded", ""]
    for r in _failed_report():
        lines.append(f"{r['name']}  ({r['provider']}, {len(r['tracks'])} track(s))")
        lines.append(f"  {r['url']}")
        for t in r["tracks"]:
            q = quote_plus(t["query"])
            lines.append(f"  #{t['index']}  {t['query']}  [{t['status']}: {t['error'][:120]}]")
            lines.append(f"      YouTube     https://www.youtube.com/results?search_query={q}")
            lines.append(f"      SoundCloud  https://soundcloud.com/search?q={q}")
        lines.append("")
    if len(lines) == 2:
        lines.append("Nothing failed. 🎉")
    return "\n".join(lines) + "\n"


# ── Library ──────────────────────────────────────────────────────────

@app.get("/api/library")
def api_library():
    """Playlists with their progress, size and cover — records merged with the
    folders on disk, so a playlist downloaded before records existed still shows."""
    folders = {d.name: d for d in DEFAULT_OUTPUT.iterdir()
               if d.is_dir() and not d.name.startswith(".") and d.name != REMOVED_DIR
               } if DEFAULT_OUTPUT.is_dir() else {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for st in StateStore(DEFAULT_OUTPUT).all():
        s = st.summary()
        folder = s.get("folder") or ""
        d = folders.get(folder)
        files, size = maintenance.folder_stats(d) if d else (0, 0)
        if d:
            seen.add(folder)
        items.append({**s, "files": files, "size_bytes": size, "has_folder": d is not None,
                      "cover": _cover_for(folder, s.get("name") or "")})
    for name, d in folders.items():
        if name in seen:
            continue
        files, size = maintenance.folder_stats(d)
        items.append({"provider": None, "key": None, "name": name, "folder": name, "url": "",
                      "files": files, "size_bytes": size, "has_folder": True,
                      "cover": _cover_for(name, name)})
    items.sort(key=lambda s: (s.get("name") or "").lower())
    removed_files = removed_bytes = 0
    for f in (DEFAULT_OUTPUT / REMOVED_DIR).rglob("*.mp3"):
        try:
            removed_bytes += f.stat().st_size
            removed_files += 1
        except OSError:
            pass
    return {"items": items,
            "total_bytes": sum(i["size_bytes"] for i in items),
            "total_files": sum(i["files"] for i in items),
            # Songs that left their playlist (kept, not deleted).
            "removed_files": removed_files, "removed_bytes": removed_bytes}


@app.get("/api/covers/{name}")
def api_cover(name: str):
    path = _safe_path(name, DEFAULT_OUTPUT / ".icons")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No cover")
    return FileResponse(str(path), headers={"Cache-Control": "max-age=86400"})


# ── System ───────────────────────────────────────────────────────────

@app.get("/api/system")
def api_system(check: bool = False):
    info = maintenance.versions()
    if check:
        info["yt_dlp_latest"] = maintenance.latest_ytdlp()
    # Providers whose last run failed far more than usual — the sign of a
    # yt-dlp that needs updating.
    info["health"] = health.assess(DEFAULT_OUTPUT, info.get("yt_dlp"), info.get("spotdl_yt_dlp"))
    info["level"] = {"enabled": loudness.enabled() and bool(info.get("ffmpeg")),
                     "target_lufs": loudness.target_lufs()}
    return info


@app.post("/api/system/update-ytdlp")
def api_update_ytdlp():
    _refuse_while_running()
    return maintenance.update_ytdlp(logging.getLogger("multi-mp3"))


@app.post("/api/system/cleanup")
def api_cleanup():
    return maintenance.cleanup(DEFAULT_OUTPUT, busy=JobManager().is_running())


@app.post("/api/system/test-notification")
def api_test_notification():
    if not os.getenv("NOTIFY_URL", "").strip():
        raise HTTPException(status_code=400, detail="Set NOTIFY_URL first")
    ok = notify("Multi-MP3", "Test notification — it works.", logging.getLogger("multi-mp3"))
    return {"sent": ok}


# ── File Browser API ─────────────────────────────────────────────────

@app.get("/api/files")
def api_list_files(path: str = ""):
    """List playlist folders in output dir."""
    base = _safe_path(path) if path else DEFAULT_OUTPUT
    if not base.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if entry.name.startswith('.'):
            continue
        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size_mb": round(entry.stat().st_size / (1024 * 1024), 1) if entry.is_file() else 0,
            "path": entry.relative_to(DEFAULT_OUTPUT).as_posix(),
            "mp3_count": len(list(entry.glob("*.mp3"))) if entry.is_dir() else 0,
        })
    return items


@app.get("/api/files/{path:path}/download")
def api_download_file(path: str):
    """Stream an MP3 file for playback/download."""
    file_path = _safe_path(path)
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        str(file_path),
        media_type="audio/mpeg",
        filename=file_path.name,
        content_disposition_type="inline",
    )
