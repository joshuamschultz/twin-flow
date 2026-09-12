"""SQLite repository for immutable operational records and snapshots."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Protocol

from twinflow.data.errors import EventConflictError, SnapshotConflictError
from twinflow.data.ingest import parse_event
from twinflow.data.models import IngestResult, OperationalEvent, Snapshot


class EventStore(Protocol):
    """Replaceable persistence boundary for normalized operational records."""

    def ingest(self, events: Iterable[OperationalEvent]) -> IngestResult: ...

    def events(
        self, *, known_at: datetime | None = None, occurred_through: datetime | None = None
    ) -> tuple[OperationalEvent, ...]: ...

    def raw_record(self, identity: tuple[str, str, str]) -> str | None: ...

    def save_snapshot(self, snapshot: Snapshot) -> None: ...

    def snapshot_document(self, snapshot_id: str) -> str | None: ...


class SQLiteEventStore:
    """Local durable store; each operation owns and closes its connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def ingest(self, events: Iterable[OperationalEvent]) -> IngestResult:
        inserted = 0
        duplicates = 0
        with self._connect() as connection:
            for event in events:
                row = connection.execute(
                    "SELECT raw_digest FROM operational_events "
                    "WHERE source=? AND event_id=? AND source_revision=?",
                    event.identity,
                ).fetchone()
                if row is not None:
                    if row[0] != event.raw_digest:
                        raise EventConflictError(
                            f"event identity has different content: {event.identity}"
                        )
                    duplicates += 1
                    continue
                connection.execute(
                    """INSERT INTO operational_events
                    (source,event_id,source_revision,occurred_at,ingested_at,entity_type,
                     entity_id,event_type,payload_json,supersedes_event_id,raw_record,raw_digest)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event.source,
                        event.event_id,
                        event.source_revision,
                        event.occurred_at.isoformat(),
                        event.ingested_at.isoformat(),
                        event.entity_type,
                        event.entity_id,
                        event.event_type,
                        json.dumps(dict(event.payload), sort_keys=True, separators=(",", ":")),
                        event.supersedes_event_id,
                        event.raw_record,
                        event.raw_digest,
                    ),
                )
                inserted += 1
        return IngestResult(inserted=inserted, duplicates=duplicates)

    def events(
        self, *, known_at: datetime | None = None, occurred_through: datetime | None = None
    ) -> tuple[OperationalEvent, ...]:
        with self._connect() as connection:
            if known_at is not None and occurred_through is not None:
                rows = connection.execute(
                    "SELECT raw_record FROM operational_events "
                    "WHERE ingested_at <= ? AND occurred_at <= ?",
                    (known_at.isoformat(), occurred_through.isoformat()),
                ).fetchall()
            elif known_at is not None:
                rows = connection.execute(
                    "SELECT raw_record FROM operational_events WHERE ingested_at <= ?",
                    (known_at.isoformat(),),
                ).fetchall()
            elif occurred_through is not None:
                rows = connection.execute(
                    "SELECT raw_record FROM operational_events WHERE occurred_at <= ?",
                    (occurred_through.isoformat(),),
                ).fetchall()
            else:
                rows = connection.execute("SELECT raw_record FROM operational_events").fetchall()
        return tuple(parse_event(json.loads(str(row[0]))) for row in rows)

    def raw_record(self, identity: tuple[str, str, str]) -> str | None:
        """Return the immutable canonical source record for lineage inspection."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT raw_record FROM operational_events "
                "WHERE source=? AND event_id=? AND source_revision=?",
                identity,
            ).fetchone()
        return str(row[0]) if row is not None else None

    def save_snapshot(self, snapshot: Snapshot) -> None:
        document = json.dumps(snapshot_to_dict(snapshot), sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM operational_snapshots WHERE snapshot_id=?",
                (snapshot.snapshot_id,),
            ).fetchone()
            if row is not None and row[0] != document:
                raise SnapshotConflictError(
                    f"snapshot ID has different content: {snapshot.snapshot_id}"
                )
            if row is None:
                connection.execute(
                    "INSERT INTO operational_snapshots(snapshot_id,document) VALUES (?,?)",
                    (snapshot.snapshot_id, document),
                )

    def snapshot_document(self, snapshot_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT document FROM operational_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return str(row[0]) if row is not None else None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operational_events (
                    source TEXT NOT NULL, event_id TEXT NOT NULL, source_revision TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL, entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL,
                    supersedes_event_id TEXT, raw_record TEXT NOT NULL, raw_digest TEXT NOT NULL,
                    PRIMARY KEY (source,event_id,source_revision)
                );
                CREATE INDEX IF NOT EXISTS operational_events_cutoff
                    ON operational_events(ingested_at, occurred_at);
                CREATE TABLE IF NOT EXISTS operational_snapshots (
                    snapshot_id TEXT PRIMARY KEY, document TEXT NOT NULL
                );
                """
            )


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, object]:
    """Convert a snapshot and nested value objects to a JSON-ready dictionary."""
    return {
        "snapshot_id": snapshot.snapshot_id,
        "known_at": snapshot.known_at.isoformat(),
        "occurred_through": snapshot.occurred_through.isoformat(),
        "source_watermarks": {
            key: value.isoformat() for key, value in snapshot.source_watermarks.items()
        },
        "entities": [
            {
                "entity_type": entity.entity_type,
                "entity_id": entity.entity_id,
                "values": dict(entity.values),
            }
            for entity in snapshot.entities
        ],
        "event_identities": [list(identity) for identity in snapshot.event_identities],
        "quality": {
            "event_count": snapshot.quality.event_count,
            "error_count": snapshot.quality.error_count,
            "diagnostics": [
                {
                    "code": item.code,
                    "severity": item.severity,
                    "message": item.message,
                    "event_identity": list(item.event_identity) if item.event_identity else None,
                }
                for item in snapshot.quality.diagnostics
            ],
        },
        "freshness": {
            "as_of": snapshot.freshness.as_of.isoformat(),
            "ready": snapshot.freshness.ready,
            "items": [
                {
                    "name": item.name,
                    "status": item.status,
                    "latest_occurred_at": item.latest_occurred_at.isoformat()
                    if item.latest_occurred_at
                    else None,
                    "age_seconds": item.age_seconds,
                }
                for item in snapshot.freshness.items
            ],
        },
        "ready": snapshot.ready,
    }
