"""Multi-MP3 Web UI — FastAPI app with REST API + SSE + Jinja2 templates."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from src.utils import clean_url, get_spotify_creds, provider_for
from src.coordinator import Coordinator
from src.event_bus import EventBus
from src.web_progress import WebProgress
from src.web_handler import SSELogHandler
from src.job_manager import JobManager

load_dotenv()

app = FastAPI(title="Multi-MP3", version="2.0.0")

# ── Static files & templates ─────────────────────────────────────────
static_dir = Path(__file__).parent / "static"
templates_dir = Path(__file__).parent / "templates"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

DEFAULT_OUTPUT = Path(os.getenv("OUTPUT_DIR", "downloads")).resolve()
DEFAULT_LINKS = Path(os.getenv("LINKS_FILE", "links.txt"))

# ── Helpers ──────────────────────────────────────────────────────────

def _get_jinja2():
    try:
        from jinja2 import Environment, FileSystemLoader
        return Environment(loader=FileSystemLoader(str(templates_dir)))
    except ImportError:
        return None


def render_template(name: str, **kwargs: Any) -> HTMLResponse:
    env = _get_jinja2()
    if env is None:
        return HTMLResponse("<h1>Jinja2 not installed</h1>", status_code=500)
    tmpl = env.get_template(name)
    return HTMLResponse(tmpl.render(**kwargs))


def parse_link_text(text: str) -> List[str]:
    """Parse links from plain text input."""
    links: List[str] = []
    for line in text.splitlines():
        url = clean_url(line)
        if url:
            links.append(url)
    return links


def parse_link_counts(text: str) -> Dict[str, int]:
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


def run_download_job(job_id: str, links: List[str], providers: List[str],
                     parallel: bool, output_dir: Path,
                     bus: EventBus) -> None:
    """Background thread: run coordinator and push lifecycle events."""
    mgr = JobManager()

    # Setup logging with SSE handler
    logger = logging.getLogger(f"job_{job_id}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()  # Avoid duplicate handlers on repeat runs
    logger.propagate = False
    logger.addHandler(SSELogHandler(bus))
    logger.addHandler(logging.StreamHandler())

    bus.push(json.dumps({"type": "job.started", "job_id": job_id}))

    try:
        client_id, client_secret = get_spotify_creds(logger)
    except ValueError:
        logger.error("Missing Spotify credentials")
        mgr.complete_job(1)
        bus.push(json.dumps({
            "type": "job.failed", "job_id": job_id,
            "error": "Missing Spotify credentials",
        }))
        return

    # Write links to temp file
    input_file = output_dir / ".web" / f"input_{job_id}.txt"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_text("\n".join(links), encoding="utf-8")

    web_progress = WebProgress(bus)

    coord = Coordinator(
        output_dir, logger, client_id, client_secret,
        parallel=parallel, max_workers=4, progress=web_progress,
    )

    try:
        exit_code = coord.process_all(input_file, providers)
    except Exception as e:
        logger.error(f"Job crashed: {e}")
        exit_code = 1

    mgr.complete_job(exit_code)

    bus.push(json.dumps({
        "type": "job.completed" if exit_code == 0 else "job.failed",
        "job_id": job_id,
        "exit_code": exit_code,
    }))


# ── Pages ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    return render_template("index.html")


@app.get("/playlist/{name:path}", response_class=HTMLResponse)
async def playlist_page(name: str):
    playlist_dir = DEFAULT_OUTPUT / name
    tracks: List[Dict[str, Any]] = []
    if playlist_dir.is_dir():
        for f in sorted(playlist_dir.iterdir()):
            if f.suffix.lower() == ".mp3":
                tracks.append({
                    "name": f.stem,
                    "path": str(f.relative_to(DEFAULT_OUTPUT)),
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                })
    return render_template("playlist.html", name=name, tracks=tracks)


# ── REST API ─────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    mgr = JobManager()
    creds_ok = bool(os.getenv("CLIENTID") and os.getenv("CLIENTSECRET"))
    return {
        "running": mgr.is_running(),
        "current_job_id": mgr.current_job_id,
        "spotify_creds": creds_ok,
        "providers": ["spotify", "youtube", "soundcloud"],
        "output_dir": str(DEFAULT_OUTPUT),
    }


@app.get("/api/links")
async def api_get_links():
    links_text = ""
    if DEFAULT_LINKS.is_file():
        links_text = DEFAULT_LINKS.read_text(encoding="utf-8")

    return {"text": links_text, "parsed": parse_link_counts(links_text)}


@app.post("/api/links/parse")
async def api_parse_links(data: Dict[str, str]):
    """Count links in text the dashboard hasn't saved yet.

    The preview re-counts as the box is typed in, so it works on the posted
    text rather than the file on disk.
    """
    return {"parsed": parse_link_counts(data.get("text", ""))}


@app.put("/api/links")
async def api_put_links(data: Dict[str, str]):
    text = data.get("text", "")
    DEFAULT_LINKS.write_text(text, encoding="utf-8")
    return {"saved": True}


@app.post("/api/jobs")
async def api_create_job(data: Dict[str, Any]):
    mgr = JobManager()
    if mgr.is_running():
        raise HTTPException(status_code=409, detail="A download job is already running")

    links_text = data.get("links", "")
    links = parse_link_text(links_text) if links_text else parse_link_text(
        DEFAULT_LINKS.read_text(encoding="utf-8") if DEFAULT_LINKS.is_file() else ""
    )

    if not links:
        raise HTTPException(status_code=400, detail="No valid links provided")

    providers = data.get("providers", ["soundcloud", "youtube", "spotify"])
    parallel = data.get("parallel", True)

    # Group links by provider so the frontend can pre-render all cards.
    # Unticking a provider drops its links here rather than only downstream:
    # the coordinator already skipped them, but they still reached the job and
    # the response, so the Live tab rendered cards that sat "Queued" forever.
    links_by_provider: Dict[str, List[str]] = {"spotify": [], "youtube": [], "soundcloud": []}
    for link in links:
        provider = provider_for(link)
        if provider in links_by_provider and provider in providers:
            links_by_provider[provider].append(link)

    links = [link for bucket in links_by_provider.values() for link in bucket]
    if not links:
        raise HTTPException(
            status_code=400,
            detail=f"No links for the selected provider(s): {', '.join(providers) or 'none'}",
        )

    job = mgr.create_job(links, providers, parallel)

    # Thread-safe event bus (asyncio.Queue is unsafe across threads)
    bus = EventBus(maxlen=2000)

    app.state.buses = getattr(app.state, "buses", {})
    app.state.buses[job.job_id] = bus

    # Set history path
    mgr.set_history_path(DEFAULT_OUTPUT / ".web" / "jobs.json")

    # Launch in background thread
    thread = threading.Thread(
        target=run_download_job,
        args=(job.job_id, links, providers, parallel, DEFAULT_OUTPUT, bus),
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job.job_id,
        "status": "running",
        "links": links_by_provider,
        "total_links": len(links),
    }


@app.get("/api/jobs")
async def api_list_jobs():
    mgr = JobManager()
    mgr.set_history_path(DEFAULT_OUTPUT / ".web" / "jobs.json")
    return mgr.get_jobs()


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    mgr = JobManager()
    job = mgr.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
async def api_cancel_job(job_id: str):
    mgr = JobManager()
    if mgr.current_job_id != job_id:
        raise HTTPException(status_code=400, detail="Job is not currently running")
    mgr.cancel_job()
    return {"cancelled": True}


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str) -> StreamingResponse:
    """SSE stream of real-time job events.

    Drains the thread-safe EventBus in batches on a fixed tick rather than
    awaiting one event at a time — this collapses bursty subprocess output
    into far fewer network writes and browser wakeups.
    """
    buses: Dict[str, EventBus] = getattr(app.state, "buses", {})
    bus = buses.get(job_id)
    if bus is None:
        async def empty():
            yield f"data: {json.dumps({'type': 'done', 'reason': 'no stream'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    async def event_stream() -> AsyncGenerator[str, None]:
        mgr = JobManager()
        idle_ticks = 0
        TICK = 0.2          # seconds between drains
        PING_AFTER = 75     # ~15s of idle ticks before a keepalive

        try:
            while True:
                batch = bus.drain(limit=200)

                if batch:
                    idle_ticks = 0
                    # Coalesce the batch into a single network write
                    yield "".join(f"data: {item}\n\n" for item in batch)
                else:
                    idle_ticks += 1
                    if idle_ticks >= PING_AFTER:
                        idle_ticks = 0
                        yield f"data: {json.dumps({'type': 'ping'})}\n\n"

                    # Only check job status when the bus is quiet
                    job = mgr.get_job(job_id)
                    if job and job.status in ("completed", "failed", "cancelled"):
                        # Flush anything that landed during the check
                        tail = bus.drain(limit=500)
                        if tail:
                            yield "".join(f"data: {item}\n\n" for item in tail)
                        yield f"data: {json.dumps({'type': 'done', 'status': job.status})}\n\n"
                        break

                await asyncio.sleep(TICK)
        finally:
            bus.close()
            buses.pop(job_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── File Browser API ─────────────────────────────────────────────────

@app.get("/api/files")
async def api_list_files(path: str = ""):
    """List playlist folders in output dir."""
    base = DEFAULT_OUTPUT / path if path else DEFAULT_OUTPUT
    if not base.is_dir():
        return []

    items: List[Dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if entry.name.startswith('.'):
            continue
        items.append({
            "name": entry.name,
            "is_dir": entry.is_dir(),
            "size_mb": round(entry.stat().st_size / (1024 * 1024), 1) if entry.is_file() else 0,
            "path": str(entry.relative_to(DEFAULT_OUTPUT)),
            "mp3_count": len(list(entry.glob("*.mp3"))) if entry.is_dir() else 0,
        })
    return items


@app.get("/api/files/{path:path}/download")
async def api_download_file(path: str):
    """Stream an MP3 file for playback/download."""
    file_path = DEFAULT_OUTPUT / path
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        str(file_path),
        media_type="audio/mpeg",
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )
