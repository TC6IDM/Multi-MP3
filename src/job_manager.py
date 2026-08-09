"""Job manager — tracks download runs, enforces one-at-a-time, persists history."""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class Job:
    job_id: str
    status: str  # "running", "completed", "failed", "cancelled"
    links: List[str] = field(default_factory=list)
    providers: List[str] = field(default_factory=list)
    parallel: bool = False
    created_at: float = 0.0
    finished_at: float | None = None
    exit_code: int | None = None
    missing_tracks: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "links": self.links,
            "providers": self.providers,
            "parallel": self.parallel,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "missing_tracks": self.missing_tracks,
        }


class JobManager:
    """Thread-safe singleton that manages download job lifecycle."""

    _instance: JobManager | None = None
    _lock = threading.Lock()

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
        self._jobs: Dict[str, Job] = {}
        self._current_job_id: str | None = None
        self._cancel_event = threading.Event()
        self._history_path: Path | None = None

    def set_history_path(self, path: Path) -> None:
        self._history_path = path
        self._load_history()

    def _load_history(self) -> None:
        if self._history_path and self._history_path.exists():
            try:
                data = json.loads(self._history_path.read_text())
                for jd in data:
                    job = Job(**jd)
                    self._jobs[job.job_id] = job
            except Exception:
                pass

    def _save_history(self) -> None:
        if self._history_path:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                data = [j.to_dict() for j in self._jobs.values()]
                self._history_path.write_text(json.dumps(data, indent=2))
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

    def create_job(self, links: List[str], providers: List[str],
                   parallel: bool = False) -> Job:
        if self.is_running():
            raise RuntimeError("A download is already running")

        job = Job(
            job_id=uuid.uuid4().hex[:12],
            status="running",
            links=links,
            providers=providers,
            parallel=parallel,
            created_at=time.time(),
        )
        self._cancel_event.clear()

        with self._lock:
            self._current_job_id = job.job_id
            self._jobs[job.job_id] = job

        return job

    def complete_job(self, exit_code: int, missing_tracks: int = 0) -> None:
        with self._lock:
            if self._current_job_id and self._current_job_id in self._jobs:
                job = self._jobs[self._current_job_id]
                job.status = "completed" if exit_code == 0 else "failed"
                job.exit_code = exit_code
                job.missing_tracks = missing_tracks
                job.finished_at = time.time()
                self._current_job_id = None
        self._save_history()

    def cancel_job(self) -> None:
        self._cancel_event.set()
        with self._lock:
            if self._current_job_id and self._current_job_id in self._jobs:
                job = self._jobs[self._current_job_id]
                job.status = "cancelled"
                job.finished_at = time.time()
                self._current_job_id = None
        self._save_history()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def get_jobs(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [j.to_dict() for j in sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )]
