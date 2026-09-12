"""Durable approval and transactional-outbox contracts for controlled actions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, cast

JsonScalar = str | int | float | bool | None
OutboxStatus = Literal["pending", "delivering", "sent", "uncertain", "conflict", "rejected"]


class ActionError(ValueError):
    """Base rejection for controlled operational actions."""


class ApprovalError(ActionError):
    """Approval is missing, expired, revoked, consumed, or out of scope."""


class RevisionConflictError(ActionError):
    """Operational state changed since proposal creation."""


@dataclass(frozen=True, slots=True)
class Proposal:
    """Canonical action proposal tied to simulation evidence and expected state."""

    proposal_id: str
    scenario_id: str
    result_id: str
    expected_operational_revision: str
    action_json: str
    digest: str

    @classmethod
    def create(
        cls,
        proposal_id: str,
        scenario_id: str,
        result_id: str,
        expected_operational_revision: str,
        action: Mapping[str, JsonScalar],
    ) -> Proposal:
        action_json = _canonical_json(dict(action))
        payload = {
            "proposal_id": proposal_id,
            "scenario_id": scenario_id,
            "result_id": result_id,
            "expected_operational_revision": expected_operational_revision,
            "action": json.loads(action_json),
        }
        digest = f"sha256:{hashlib.sha256(_canonical_json(payload).encode()).hexdigest()}"
        return cls(
            proposal_id,
            scenario_id,
            result_id,
            expected_operational_revision,
            action_json,
            digest,
        )

    @property
    def action(self) -> dict[str, JsonScalar]:
        return cast(dict[str, JsonScalar], json.loads(self.action_json))


@dataclass(frozen=True, slots=True)
class Approval:
    """Human approval for one exact proposal digest and action-type scope."""

    approval_id: str
    proposal_digest: str
    approved_by: str
    scopes: frozenset[str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """One durable action awaiting or recording downstream delivery."""

    entry_id: str
    idempotency_key: str
    proposal: Proposal
    approval_id: str
    status: OutboxStatus
    attempts: int


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    """Downstream response. `confirmed=False` requires reconciliation."""

    receipt_id: str
    confirmed: bool
    downstream_revision: str | None
    detail: str


class ActionSink(Protocol):
    """Configured authorized boundary that receives one idempotent outbox entry."""

    def send(self, entry: OutboxEntry) -> DispatchReceipt: ...


class DryRunSink:
    """Deterministic sink that records calls and performs no external write."""

    def __init__(self, *, confirmed: bool = True) -> None:
        self.confirmed = confirmed
        self.calls: list[str] = []

    def send(self, entry: OutboxEntry) -> DispatchReceipt:
        self.calls.append(entry.idempotency_key)
        return DispatchReceipt(
            receipt_id=f"dry-run:{entry.idempotency_key}",
            confirmed=self.confirmed,
            downstream_revision=(
                f"dry-run-after:{entry.proposal.expected_operational_revision}"
                if self.confirmed
                else None
            ),
            detail="dry run; no external operation performed",
        )


class TransactionalOutbox:
    """SQLite approval consumption, idempotency, delivery, and restart recovery."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def register_approval(self, approval: Approval) -> None:
        if approval.expires_at.tzinfo is None:
            raise ApprovalError("approval expiry must be timezone-aware")
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO approvals
                   (approval_id, proposal_digest, approved_by, scopes_json,
                    expires_at, revoked, consumed)
                   VALUES (?, ?, ?, ?, ?, 0, 0)""",
                (
                    approval.approval_id,
                    approval.proposal_digest,
                    approval.approved_by,
                    _canonical_json(sorted(approval.scopes)),
                    approval.expires_at.astimezone(UTC).isoformat(),
                ),
            )

    def revoke(self, approval_id: str) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE approvals SET revoked = 1 WHERE approval_id = ?", (approval_id,)
            )
            if cursor.rowcount != 1:
                raise ApprovalError(f"unknown approval {approval_id!r}")

    def submit(
        self,
        proposal: Proposal,
        approval_id: str,
        idempotency_key: str,
        *,
        now: datetime,
    ) -> OutboxEntry:
        """Atomically validate/consume approval and create one idempotent entry."""
        if now.tzinfo is None:
            raise ApprovalError("current time must be timezone-aware")
        action_type = proposal.action.get("type")
        if not isinstance(action_type, str):
            raise ActionError("action requires a string type")
        if (
            Proposal.create(
                proposal.proposal_id,
                proposal.scenario_id,
                proposal.result_id,
                proposal.expected_operational_revision,
                proposal.action,
            )
            != proposal
        ):
            raise ActionError("proposal canonical payload or digest is invalid")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT entry_id, proposal_json, approval_id FROM outbox
                   WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing[1]) != _proposal_json(proposal) or str(existing[2]) != approval_id:
                    raise ActionError("idempotency key was already used for a different request")
                return self.get(str(existing[0]), connection=connection)
            row = connection.execute(
                """SELECT proposal_digest, scopes_json, expires_at, revoked, consumed
                   FROM approvals WHERE approval_id = ?""",
                (approval_id,),
            ).fetchone()
            if row is None:
                raise ApprovalError(f"unknown approval {approval_id!r}")
            digest, scopes_json, expires_at, revoked, consumed = row
            if bool(revoked):
                raise ApprovalError("approval is revoked")
            if bool(consumed):
                raise ApprovalError("approval was already consumed")
            if str(digest) != proposal.digest:
                raise ApprovalError("approval digest does not match proposal")
            if now.astimezone(UTC) >= datetime.fromisoformat(str(expires_at)):
                raise ApprovalError("approval is expired")
            scopes = cast(list[str], json.loads(str(scopes_json)))
            if action_type not in scopes:
                raise ApprovalError(f"action type {action_type!r} is outside approval scope")
            entry_id = f"outbox-{uuid.uuid4().hex}"
            connection.execute(
                """INSERT INTO outbox
                   (entry_id, idempotency_key, proposal_json, approval_id, status, attempts)
                   VALUES (?, ?, ?, ?, 'pending', 0)""",
                (entry_id, idempotency_key, _proposal_json(proposal), approval_id),
            )
            consumed_cursor = connection.execute(
                "UPDATE approvals SET consumed = 1 WHERE approval_id = ? AND consumed = 0",
                (approval_id,),
            )
            if consumed_cursor.rowcount != 1:
                raise ApprovalError("approval was concurrently consumed")
            return self.get(entry_id, connection=connection)

    def dispatch_pending(
        self,
        sink: ActionSink,
        *,
        current_operational_revision: str,
    ) -> list[DispatchReceipt]:
        """Claim pending entries, revision-check, send, and persist receipts."""
        receipts: list[DispatchReceipt] = []
        for entry in self.list_entries(status="pending"):
            if entry.proposal.expected_operational_revision != current_operational_revision:
                self._set_status(entry.entry_id, "conflict", only_from="pending")
                continue
            if not self._set_status(
                entry.entry_id, "delivering", increment_attempt=True, only_from="pending"
            ):
                continue
            entry = self.get(entry.entry_id)
            receipt = sink.send(entry)
            status: OutboxStatus = "sent" if receipt.confirmed else "uncertain"
            with self._connection() as connection:
                connection.execute(
                    """INSERT OR REPLACE INTO receipts
                       (entry_id, receipt_id, confirmed, downstream_revision, detail)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        entry.entry_id,
                        receipt.receipt_id,
                        int(receipt.confirmed),
                        receipt.downstream_revision,
                        receipt.detail,
                    ),
                )
                connection.execute(
                    "UPDATE outbox SET status = ? WHERE entry_id = ?", (status, entry.entry_id)
                )
            receipts.append(receipt)
        return receipts

    def recover(self) -> int:
        """Return interrupted `delivering` entries to pending after restart."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE outbox SET status = 'pending' WHERE status = 'delivering'"
            )
            return cursor.rowcount

    def reconcile(self, entry_id: str, receipt: DispatchReceipt) -> None:
        """Resolve an uncertain delivery using an externally obtained receipt."""
        entry = self.get(entry_id)
        if entry.status != "uncertain":
            raise ActionError("only uncertain entries may be reconciled")
        status: OutboxStatus = "sent" if receipt.confirmed else "rejected"
        with self._connection() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO receipts
                   (entry_id, receipt_id, confirmed, downstream_revision, detail)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    entry_id,
                    receipt.receipt_id,
                    int(receipt.confirmed),
                    receipt.downstream_revision,
                    receipt.detail,
                ),
            )
            connection.execute(
                "UPDATE outbox SET status = ? WHERE entry_id = ?", (status, entry_id)
            )

    def get(self, entry_id: str, *, connection: sqlite3.Connection | None = None) -> OutboxEntry:
        owns_connection = connection is None
        active = connection or self._connect()
        try:
            row = active.execute(
                """SELECT entry_id, idempotency_key, proposal_json, approval_id, status, attempts
                   FROM outbox WHERE entry_id = ?""",
                (entry_id,),
            ).fetchone()
            if row is None:
                raise ActionError(f"unknown outbox entry {entry_id!r}")
            return OutboxEntry(
                entry_id=str(row[0]),
                idempotency_key=str(row[1]),
                proposal=_proposal_from_json(str(row[2])),
                approval_id=str(row[3]),
                status=cast(OutboxStatus, row[4]),
                attempts=int(row[5]),
            )
        finally:
            if owns_connection:
                active.close()

    def list_entries(self, *, status: OutboxStatus | None = None) -> list[OutboxEntry]:
        with self._connection() as connection:
            if status is None:
                rows = connection.execute("SELECT entry_id FROM outbox ORDER BY rowid").fetchall()
            else:
                rows = connection.execute(
                    "SELECT entry_id FROM outbox WHERE status = ? ORDER BY rowid", (status,)
                ).fetchall()
            return [self.get(str(row[0]), connection=connection) for row in rows]

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY, proposal_digest TEXT NOT NULL,
                    approved_by TEXT NOT NULL, scopes_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL, revoked INTEGER NOT NULL, consumed INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    entry_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                    proposal_json TEXT NOT NULL, approval_id TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL,
                    FOREIGN KEY (approval_id) REFERENCES approvals(approval_id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    entry_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL,
                    confirmed INTEGER NOT NULL, downstream_revision TEXT, detail TEXT NOT NULL,
                    FOREIGN KEY (entry_id) REFERENCES outbox(entry_id)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _set_status(
        self,
        entry_id: str,
        status: OutboxStatus,
        *,
        increment_attempt: bool = False,
        only_from: OutboxStatus | None = None,
    ) -> bool:
        with self._connection() as connection:
            query = "UPDATE outbox SET status = ?, attempts = attempts + ? WHERE entry_id = ?"
            parameters: tuple[object, ...] = (status, int(increment_attempt), entry_id)
            if only_from is not None:
                query += " AND status = ?"
                parameters += (only_from,)
            return connection.execute(query, parameters).rowcount == 1


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _proposal_json(proposal: Proposal) -> str:
    return _canonical_json(
        {
            "proposal_id": proposal.proposal_id,
            "scenario_id": proposal.scenario_id,
            "result_id": proposal.result_id,
            "expected_operational_revision": proposal.expected_operational_revision,
            "action_json": proposal.action_json,
            "digest": proposal.digest,
        }
    )


def _proposal_from_json(value: str) -> Proposal:
    payload = cast(dict[str, str], json.loads(value))
    return Proposal(**payload)
