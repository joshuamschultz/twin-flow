"""A small durable local repository; transport and engine have no SQL dependencies."""

from __future__ import annotations

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

    def claim_job(self, key: str, digest: str, job: dict[str, Any]) -> tuple[str, bool]:
        """Atomically publish a job and claim its request key."""
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,job_id FROM requests WHERE key=?", (key,)).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError("Request key was used with different inputs")
                return str(row[1]), False
            job_id = str(job["id"])
            db.execute(
                "INSERT INTO records(kind,id,body) VALUES(?,?,?)",
                ("jobs", job_id, json.dumps(job, allow_nan=False)),
            )
            db.execute(
                "INSERT INTO requests(key,digest,job_id) VALUES(?,?,?)", (key, digest, job_id)
            )
        return job_id, True
