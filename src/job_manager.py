"""Job manager — runs one download job at a time, queues the rest, keeps history."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

FINISHED = ("completed", "partial", "failed", "cancelled")


@dataclass
class Job:
    job_id: str
    # "queued", "running", "cancelling", "completed", "partial", "failed", "cancelled"
    status: str
    links: list[str] = field(default_factory=list)
    providers: list[str] = field(default_factory=list)
    parallel: bool = False
    speed: str = "balanced"
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    missing_tracks: int = 0
    # Track counts across the run's playlists, from their progress records.
    done: int = 0
    failed: int = 0
    pending: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class JobManager:
    """Thread-safe singleton that manages the job lifecycle.

    Only one job downloads at a time — two at once would double the request
    rate the limiters are carefully managing. Starting another while one runs
    used to be refused (HTTP 409); now it waits its turn in a queue.
    """

    _instance: JobManager | None = None
    _lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls) -> JobManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._current_job_id: str | None = None
        self._cancel_event = threading.Event()
        self._history_path: Path | None = None

    def set_history_path(self, path: Path) -> None:
        self._history_path = path
        self._load_history()

    def _load_history(self) -> None:
        if self._history_path and self._history_path.exists():
            try:
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                known = {f.name for f in fields(Job)}
                with self._lock:
                    for jd in data:
                        job = Job(**{k: v for k, v in jd.items() if k in known})
                        # Never let a reload clobber a live job. A job saved as
                        # queued/running by a server that has since stopped
                        # will never run: show it as cancelled.
                        if job.job_id in self._jobs and self._jobs[job.job_id].status not in FINISHED:
                            continue
                        if job.status not in FINISHED:
                            job.status = "cancelled"
                        self._jobs[job.job_id] = job
            except Exception:
                pass

    def _save_history(self) -> None:
        if self._history_path:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                with self._lock:
                    data = [j.to_dict() for j in self._jobs.values()]
                self._history_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass

    @property
    def current_job_id(self) -> str | None:
        with self._lock:
            return self._current_job_id

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def is_running(self) -> bool:
        with self._lock:
            return self._current_job_id is not None

    def queued(self) -> list[Job]:
        with self._lock:
            return [self._jobs[j] for j in self._queue if j in self._jobs]

    def create_job(self, links: list[str], providers: list[str],
                   speed: str = "balanced") -> Job:
        """A new job: running straight away if nothing is, otherwise queued."""
        with self._lock:
            job = Job(
                job_id=uuid.uuid4().hex[:12],
                status="queued",
                links=links,
                providers=providers,
                parallel=speed != "careful",
                speed=speed,
                created_at=time.time(),
            )
            self._jobs[job.job_id] = job
            if self._current_job_id is None:
                self._start(job)
            else:
                self._queue.append(job.job_id)
        return job

    def _start(self, job: Job) -> None:
        self._cancel_event.clear()
        self._current_job_id = job.job_id
        job.status = "running"
        job.started_at = time.time()

    def start_next(self) -> Job | None:
        """Promote the next queued job, if nothing is running."""
        with self._lock:
            if self._current_job_id is not None:
                return None
            while self._queue:
                job = self._jobs.get(self._queue.popleft())
                if job is not None and job.status == "queued":
                    self._start(job)
                    return job
        return None

    def queue_position(self, job_id: str) -> int:
        with self._lock:
            try:
                return list(self._queue).index(job_id) + 1
            except ValueError:
                return 0

    def complete_job(self, exit_code: int, counts: dict[str, int] | None = None) -> Job | None:
        """Called by the job's own thread once every download has stopped."""
        counts = counts or {}
        finished = None
        with self._lock:
            if self._current_job_id and self._current_job_id in self._jobs:
                job = self._jobs[self._current_job_id]
                if self._cancel_event.is_set():
                    job.status = "cancelled"
                elif exit_code == 0:
                    job.status = "completed"
                elif exit_code == 2:
                    job.status = "partial"     # some tracks left to do
                else:
                    job.status = "failed"
                job.exit_code = exit_code
                job.done = int(counts.get("done", 0))
                job.failed = int(counts.get("failed", 0))
                job.pending = int(counts.get("pending", 0))
                job.total = int(counts.get("total", 0))
                job.missing_tracks = job.failed + job.pending
                job.finished_at = time.time()
                self._current_job_id = None
                finished = job
        self._save_history()
        return finished

    def cancel_job(self, job_id: str | None = None) -> bool:
        """Stop the running job, or take a queued one off the queue.

        A running job stays current — status "cancelling" — until its thread
        has actually stopped and called complete_job(). Clearing it here, as
        this used to, let a second job start while the first still downloaded.
        """
        with self._lock:
            target = job_id or self._current_job_id
            if target is None or target not in self._jobs:
                return False
            job = self._jobs[target]
            if target == self._current_job_id:
                self._cancel_event.set()
                job.status = "cancelling"
            elif job.status == "queued":
                try:
                    self._queue.remove(target)
                except ValueError:
                    pass
                job.status = "cancelled"
                job.finished_at = time.time()
            else:
                return False
        self._save_history()
        return True

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )]
