"""A small durable local repository; transport and engine have no SQL dependencies."""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast


class WorkspaceRepository:
    """Store JSON records and atomic job idempotency claims in one local workspace.

    One workspace belongs to one trusted local operator. Remote tenant isolation is
    the responsibility of a separately configured deployment boundary.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "workspace.sqlite3"
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, "
                "created INTEGER PRIMARY KEY AUTOINCREMENT, UNIQUE(kind,id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS requests ("
                "key TEXT PRIMARY KEY, digest TEXT NOT NULL, job_id TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL, "
                "operation TEXT NOT NULL, object_kind TEXT NOT NULL, object_id TEXT NOT NULL, "
                "outcome TEXT NOT NULL, correlation_id TEXT)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        """Insert an immutable identity, refusing replacement."""
        try:
            with self._connection() as db:
                db.execute(
                    "INSERT INTO records(kind,id,body) VALUES(?,?,?)",
                    (kind, record_id, json.dumps(payload, allow_nan=False)),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Record already exists") from exc

    def get(self, kind: str, record_id: str) -> dict[str, Any]:
        """Get one scoped record; returned JSON is detached from storage."""
        with self._connection() as db:
            row = db.execute(
                "SELECT body FROM records WHERE kind=? AND id=?", (kind, record_id)
            ).fetchone()
        if row is None:
            raise KeyError(record_id)
        return cast(dict[str, Any], json.loads(row[0]))

    def list(self, kind: str, limit: int = 100) -> list[dict[str, Any]]:
        """Newest first, with an explicit result bound."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT body FROM records WHERE kind=? ORDER BY created DESC LIMIT ?",
                (kind, max(1, min(limit, 1000))),
            ).fetchall()
        return [cast(dict[str, Any], json.loads(row[0])) for row in rows]

    def count(self, kind: str) -> int:
        """Count records of one fixed application kind."""
        with self._connection() as db:
            row = db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()
        return int(row[0]) if row is not None else 0

    def update(self, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        """Replace mutable job/draft state; scenario callers use create only."""
        if kind == "scenarios":
            raise ValueError("Scenario versions are immutable")
        with self._connection() as db:
            result = db.execute(
                "UPDATE records SET body=? WHERE kind=? AND id=?",
                (json.dumps(payload, allow_nan=False), kind, record_id),
            )
            if result.rowcount != 1:
                raise KeyError(record_id)

    def claim_job(
        self, key: str, digest: str, job: dict[str, Any], max_active_jobs: int = 4
    ) -> tuple[str, bool]:
        """Atomically replay a claim or admit a new job under the durable queue bound."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,job_id FROM requests WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Request key was used with different inputs")
                return str(row[1]), False
            active = db.execute(
                "SELECT COUNT(*) FROM records WHERE kind='jobs' "
                "AND json_extract(body, '$.status') IN ('queued','running','cancel_requested')"
            ).fetchone()
            if active is not None and int(active[0]) >= max_active_jobs:
                raise ValueError("Workspace compute queue is full; wait for an existing job")
            job_id = str(job["id"])
            db.execute(
                "INSERT INTO records(kind,id,body) VALUES(?,?,?)",
                ("jobs", job_id, json.dumps(job, allow_nan=False)),
            )
            db.execute(
                "INSERT INTO requests(key,digest,job_id) VALUES(?,?,?)", (key, digest, job_id)
            )
        return job_id, True

    def transition_job(
        self,
        job_id: str,
        expected_statuses: tuple[str, ...],
        changes: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Compare-and-set a job while holding the SQLite write lock."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT body FROM records WHERE kind='jobs' AND id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = cast(dict[str, Any], json.loads(row[0]))
            if str(job.get("status")) not in expected_statuses:
                return None
            job.update(changes)
            db.execute(
                "UPDATE records SET body=? WHERE kind='jobs' AND id=?",
                (json.dumps(job, allow_nan=False), job_id),
            )
            return job

    def recover_jobs(self, finished_at: str) -> int:
        """Resolve nonterminal jobs left by the previous owning process."""
        recovered = 0
        with self._connection() as db:
            rows = db.execute("SELECT body FROM records WHERE kind='jobs' AND "
                "json_extract(body, '$.status') IN ('queued','running','cancel_requested')").fetchall()
        for row in rows:
            job = json.loads(row[0])
            status = str(job.get("status"))
            if status in ("queued", "running"):
                changes: dict[str, Any] = {
                    "status": "interrupted",
                    "error": "Service restarted before result publication",
                    "finished_at": finished_at,
                }
            elif status == "cancel_requested":
                changes = {"status": "canceled", "error": None, "finished_at": finished_at}
            else:
                continue
            if self.transition_job(str(job["id"]), (status,), changes) is not None:
                recovered += 1
        return recovered

    def append_audit(
        self,
        occurred_at: str,
        operation: str,
        object_kind: str,
        object_id: str,
        outcome: str,
        correlation_id: str | None = None,
    ) -> None:
        """Append one bounded service operation record."""
        with self._connection() as db:
            db.execute(
                "INSERT INTO audit_events"
                "(occurred_at,operation,object_kind,object_id,outcome,correlation_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    occurred_at,
                    operation[:80],
                    object_kind[:80],
                    object_id[:256],
                    outcome[:80],
                    correlation_id,
                ),
            )

    def audit_events(self, limit: int = 100) -> builtins.list[dict[str, object]]:
        """Return recent audit metadata without exposing arbitrary SQL."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT occurred_at,operation,object_kind,object_id,outcome,correlation_id "
                "FROM audit_events ORDER BY sequence DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [
            {
                "occurred_at": row[0],
                "operation": row[1],
                "object_kind": row[2],
                "object_id": row[3],
                "outcome": row[4],
                "correlation_id": row[5],
            }
            for row in rows
        ]

    def backup_to(self, destination: str | Path) -> None:
        """Create a transactionally consistent SQLite copy using the backup API."""
        target = Path(destination)
        with self._connection() as source, sqlite3.connect(target) as backup:
            source.backup(backup)
