"""A small in-memory job store — run / sweep / optimize are long, so the API launches
them and the front end polls.

A job is submitted with a callable returning a JSON-friendly dict; it runs on a
background thread (the engine's own parallelism is process-based, so a worker thread
only waits on it). The store is deliberately in-memory and single-process: this is a
local alpha driver for one operator's twin, not a multi-tenant service. Restarting the
API forgets finished jobs — the run artifacts on disk are the durable record.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

JobStatus = Literal["running", "done", "error"]


@dataclass
class Job:
    """One submitted unit of work and its evolving state."""

    id: str
    kind: str
    status: JobStatus = "running"
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


@dataclass
class JobStore:
    """Thread-safe registry of background jobs, keyed by id."""

    max_workers: int = 2
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _executor: ThreadPoolExecutor | None = None

    def _pool(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.max_workers)
        return self._executor

    def submit(self, kind: str, work: Callable[[], dict[str, Any]]) -> Job:
        """Register a `running` job and dispatch `work` to a background thread."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind)
        with self._lock:
            self._jobs[job.id] = job
        self._pool().submit(self._run, job, work)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def _run(self, job: Job, work: Callable[[], dict[str, Any]]) -> None:
        try:
            result = work()
        except Exception as exc:  # a failed job must record, never crash the worker
            with self._lock:
                job.status = "error"
                job.error = f"{exc}\n{traceback.format_exc()}"
            return
        with self._lock:
            job.status = "done"
            job.result = result
