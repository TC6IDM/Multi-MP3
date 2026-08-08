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
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from src.utils import clean_url, get_spotify_creds, read_links, setup_logging
from src.coordinator import Coordinator
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

DEFAULT_OUTPUT = Path(os.getenv("OUTPUT_DIR", "downloads"))
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


def run_download_job(job_id: str, links: List[str], providers: List[str],
                     parallel: bool, output_dir: Path,
                     event_queue: asyncio.Queue) -> None:
    """Background thread: run coordinator and push lifecycle events."""
    mgr = JobManager()

    # Setup logging with SSE handler
    logger = logging.getLogger(f"job_{job_id}")
    logger.setLevel(logging.INFO)
    sse_handler = SSELogHandler(event_queue)
    logger.addHandler(sse_handler)
    # Also keep console output
    logger.addHandler(logging.StreamHandler())

    # Push job.started
    try:
        event_queue.put_nowait(json.dumps({
            "type": "job.started", "job_id": job_id
        }))
    except asyncio.QueueFull:
        pass

    try:
        client_id, client_secret = get_spotify_creds(logger)
    except ValueError:
        logger.error("Missing Spotify credentials")
        mgr.complete_job(1)
        event_queue.put_nowait(json.dumps({
            "type": "job.failed", "job_id": job_id, "error": "Missing Spotify credentials"
        }))
        return

    # Write links to temp file
    input_file = output_dir / ".web" / f"input_{job_id}.txt"
    input_file.parent.mkdir(parents=True, exist_ok=True)
    input_file.write_text("\n".join(links))

    # Create web progress
    web_progress = WebProgress(event_queue)

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

    event_data = {
        "type": "job.completed" if exit_code == 0 else "job.failed",
        "job_id": job_id,
        "exit_code": exit_code,
    }
    try:
        event_queue.put_nowait(json.dumps(event_data))
    except asyncio.QueueFull:
        pass


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

    parsed: Dict[str, List[str]] = {"spotify": [], "youtube": [], "soundcloud": []}
    if DEFAULT_LINKS.is_file():
        parsed = read_links(DEFAULT_LINKS, logging.getLogger("api"))

    return {
        "text": links_text,
        "parsed": {
            "total": sum(len(v) for v in parsed.values()),
            "spotify": len(parsed.get("spotify", [])),
            "youtube": len(parsed.get("youtube", [])),
            "soundcloud": len(parsed.get("soundcloud", [])),
        },
    }


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

    job = mgr.create_job(links, providers, parallel)

    # SSE event queue — ring buffer of last 500 events
    event_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

    # Store queue so SSE endpoint can find it
    app.state.queues = getattr(app.state, "queues", {})
    app.state.queues[job.job_id] = event_queue

    # Set history path
    mgr.set_history_path(DEFAULT_OUTPUT / ".web" / "jobs.json")

    # Launch in background thread
    thread = threading.Thread(
        target=run_download_job,
        args=(job.job_id, links, providers, parallel, DEFAULT_OUTPUT, event_queue),
        daemon=True,
    )
    thread.start()

    return {"job_id": job.job_id, "status": "running", "links": len(links)}


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
    """SSE stream of real-time job events."""
    queues: Dict[str, asyncio.Queue] = getattr(app.state, "queues", {})
    queue = queues.get(job_id)
    if queue is None:
        # Job may have completed — return empty stream
        async def empty():
            yield f"data: {json.dumps({'type': 'done', 'reason': 'no queue'})}\n\n"
        return StreamingResponse(empty(), media_type="text/event-stream")

    # Ring buffer — store last 200 events for replay
    ring: List[str] = []
    ring_max = 200

    async def event_stream() -> AsyncGenerator[str, None]:
        # Replay ring buffer first
        for item in ring:
            yield f"data: {item}\n\n"

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=15.0)
                ring.append(data)
                if len(ring) > ring_max:
                    ring.pop(0)
                yield f"data: {data}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

            # Check if job is done
            mgr = JobManager()
            job = mgr.get_job(job_id)
            if job and job.status in ("completed", "failed", "cancelled"):
                yield f"data: {json.dumps({'type': 'done', 'status': job.status})}\n\n"
                break

        # Cleanup queue
        if job_id in queues:
            del queues[job_id]

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
