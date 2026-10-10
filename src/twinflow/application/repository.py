"""A small durable local repository; transport and engine have no SQL dependencies."""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

# Fixed literals only; never caller input (they are interpolated into ALTER TABLE).
_TOOL_AUDIT_COLUMNS = (
    ("caller_id", "TEXT"),
    ("tool", "TEXT"),
    ("request_key", "TEXT"),
    ("input_digest", "TEXT"),
    ("duration_ms", "REAL"),
)
_AUDIT_FIELDS = (
    "occurred_at",
    "operation",
    "object_kind",
    "object_id",
    "outcome",
    "correlation_id",
    *(column for column, _ in _TOOL_AUDIT_COLUMNS),
)


class WorkspaceRepository:
    """Store JSON records and atomic job idempotency claims in one local workspace.

    One workspace belongs to one trusted local operator. Remote tenant isolation is
    the responsibility of a separately configured deployment boundary.
    """

    def __init__(self, root: str | Path, *, max_records_per_kind: int = 10_000) -> None:
        self.max_records_per_kind = max_records_per_kind
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
        self._migrate_audit_events()

    def _migrate_audit_events(self) -> None:
        """Forward-only: add the tool-audit columns (COMP-122) to any audit table.

        Fresh and existing workspaces take the same path. The write lock is held while
        the columns are read, so two processes opening one workspace cannot both add one.
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            present = {row[1] for row in db.execute("PRAGMA table_info(audit_events)")}
            for column, column_type in _TOOL_AUDIT_COLUMNS:
                if column not in present:
                    db.execute(f"ALTER TABLE audit_events ADD COLUMN {column} {column_type}")

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
                db.execute("BEGIN IMMEDIATE")
                count = db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()
                if count is not None and count[0] >= self.max_records_per_kind:
                    raise ValueError("Workspace record quota reached")
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
            total = db.execute("SELECT COUNT(*) FROM records WHERE kind='jobs'").fetchone()
            if total is not None and total[0] >= self.max_records_per_kind:
                raise ValueError("Workspace record quota reached")
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

    def find_request_claim(self, key: str) -> tuple[str, str] | None:
        """The `(input digest, result id)` claimed under `key`, or None when unclaimed."""
        with self._connection() as db:
            row = db.execute("SELECT digest,job_id FROM requests WHERE key=?", (key,)).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def claim_request(
        self,
        key: str,
        digest: str,
        result_id: str,
        records: Sequence[tuple[str, str, dict[str, Any]]],
    ) -> tuple[str, str]:
        """Atomically store `records` and claim `key` for `result_id`, or keep the first claim.

        Returns the `(digest, result id)` that owns `key` after the call. When `key` was
        already claimed nothing is written and the existing claim is returned, so a caller
        that lost a race can compare digests and replay the winner's result. Unlike
        `claim_job`, the claimed result may live in any record kind, and no job is created.
        """
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest,job_id FROM requests WHERE key=?", (key,)).fetchone()
            if row:
                return str(row[0]), str(row[1])
            for kind, record_id, payload in records:
                self._insert_within_quota(db, kind, record_id, payload)
            db.execute(
                "INSERT INTO requests(key,digest,job_id) VALUES(?,?,?)", (key, digest, result_id)
            )
        return digest, result_id

    def _insert_within_quota(
        self, db: sqlite3.Connection, kind: str, record_id: str, payload: dict[str, Any]
    ) -> None:
        count = db.execute("SELECT COUNT(*) FROM records WHERE kind=?", (kind,)).fetchone()
        if count is not None and count[0] >= self.max_records_per_kind:
            raise ValueError("Workspace record quota reached")
        try:
            db.execute(
                "INSERT INTO records(kind,id,body) VALUES(?,?,?)",
                (kind, record_id, json.dumps(payload, allow_nan=False)),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("Record already exists") from exc

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
            rows = db.execute(
                "SELECT body FROM records WHERE kind='jobs' AND "
                "json_extract(body, '$.status') IN ('queued','running','cancel_requested')"
            ).fetchall()
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

    def append_tool_audit(
        self,
        *,
        occurred_at: str,
        caller_id: str,
        tool: str,
        request_key: str | None,
        input_digest: str,
        outcome: str,
        duration_ms: float,
        correlation_id: str | None,
    ) -> None:
        """Append one tool-call audit row (operation ``tool_call``, object = the tool)."""
        with self._connection() as db:
            db.execute(
                "INSERT INTO audit_events"
                "(occurred_at,operation,object_kind,object_id,outcome,correlation_id,"
                "caller_id,tool,request_key,input_digest,duration_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    occurred_at,
                    "tool_call",
                    "tool",
                    tool[:256],
                    outcome[:80],
                    correlation_id,
                    caller_id,
                    tool,
                    request_key,
                    input_digest,
                    duration_ms,
                ),
            )

    def audit_events(self, limit: int = 100) -> builtins.list[dict[str, object]]:
        """Return recent audit metadata without exposing arbitrary SQL.

        Rows written before the tool-audit migration carry None in its columns.
        """
        with self._connection() as db:
            rows = db.execute(
                f"SELECT {','.join(_AUDIT_FIELDS)} "  # noqa: S608 - fixed column literals
                "FROM audit_events ORDER BY sequence DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(zip(_AUDIT_FIELDS, row, strict=True)) for row in rows]

    def backup_to(self, destination: str | Path) -> None:
        """Create a transactionally consistent SQLite copy using the backup API."""
        target = Path(destination)
        with self._connection() as source, sqlite3.connect(target) as backup:
            source.backup(backup)
