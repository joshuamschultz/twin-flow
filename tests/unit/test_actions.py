from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from twinflow.actions import (
    ActionError,
    Approval,
    ApprovalError,
    DispatchReceipt,
    DryRunSink,
    Proposal,
    TransactionalOutbox,
)


def _proposal(revision: str = "rev-1", *, action_type: str = "release") -> Proposal:
    return Proposal.create(
        "p-1", "scenario-1", "result-1", revision, {"type": action_type, "qty": 2}
    )


def _approve(outbox: TransactionalOutbox, proposal: Proposal, approval_id: str = "a-1") -> None:
    outbox.register_approval(
        Approval(
            approval_id,
            proposal.digest,
            "operator@example.test",
            frozenset({"release"}),
            datetime.now(UTC) + timedelta(hours=1),
        )
    )


def test_approval_consumption_and_idempotency_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "actions.sqlite3"
    proposal = _proposal()
    first = TransactionalOutbox(database)
    _approve(first, proposal)
    entry = first.submit(proposal, "a-1", "request-1", now=datetime.now(UTC))

    restarted = TransactionalOutbox(database)
    duplicate = restarted.submit(proposal, "a-1", "request-1", now=datetime.now(UTC))
    sink = DryRunSink()
    receipts = restarted.dispatch_pending(sink, current_operational_revision="rev-1")

    assert duplicate.entry_id == entry.entry_id
    assert len(receipts) == 1
    assert restarted.get(entry.entry_id).status == "sent"
    assert restarted.dispatch_pending(sink, current_operational_revision="rev-1") == []
    assert sink.calls == ["request-1"]


def test_idempotency_key_cannot_alias_another_request(tmp_path: Path) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    proposal = _proposal()
    _approve(outbox, proposal)
    outbox.submit(proposal, "a-1", "same-key", now=datetime.now(UTC))
    with pytest.raises(ActionError, match="different request"):
        outbox.submit(_proposal("rev-2"), "a-1", "same-key", now=datetime.now(UTC))


@pytest.mark.parametrize("mode", ["expired", "revoked", "scope"])
def test_invalid_approval_is_rejected(tmp_path: Path, mode: str) -> None:
    outbox = TransactionalOutbox(tmp_path / f"{mode}.sqlite3")
    proposal = _proposal(action_type="hold" if mode == "scope" else "release")
    expiry = (
        datetime.now(UTC) - timedelta(seconds=1)
        if mode == "expired"
        else datetime.now(UTC) + timedelta(hours=1)
    )
    outbox.register_approval(
        Approval("a-1", proposal.digest, "operator", frozenset({"release"}), expiry)
    )
    if mode == "revoked":
        outbox.revoke("a-1")
    with pytest.raises(ApprovalError):
        outbox.submit(proposal, "a-1", "request", now=datetime.now(UTC))


def test_tampered_proposal_is_rejected(tmp_path: Path) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    proposal = _proposal()
    _approve(outbox, proposal)
    with pytest.raises(ActionError, match="invalid"):
        outbox.submit(
            replace(proposal, digest="sha256:tampered"),
            "a-1",
            "request",
            now=datetime.now(UTC),
        )


def test_revision_conflict_prevents_dispatch(tmp_path: Path) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    proposal = _proposal()
    _approve(outbox, proposal)
    entry = outbox.submit(proposal, "a-1", "request", now=datetime.now(UTC))
    sink = DryRunSink()
    assert outbox.dispatch_pending(sink, current_operational_revision="rev-2") == []
    assert outbox.get(entry.entry_id).status == "conflict"
    assert sink.calls == []


def test_interrupted_delivery_recovers_and_uncertain_receipt_reconciles(tmp_path: Path) -> None:
    class FailingSink:
        def send(self, entry: object) -> DispatchReceipt:
            del entry
            raise RuntimeError("transport interrupted")

    database = tmp_path / "actions.sqlite3"
    outbox = TransactionalOutbox(database)
    proposal = _proposal()
    _approve(outbox, proposal)
    entry = outbox.submit(proposal, "a-1", "request", now=datetime.now(UTC))
    with pytest.raises(RuntimeError, match="interrupted"):
        outbox.dispatch_pending(FailingSink(), current_operational_revision="rev-1")

    restarted = TransactionalOutbox(database)
    assert restarted.get(entry.entry_id).status == "delivering"
    assert restarted.recover() == 1
    receipts = restarted.dispatch_pending(
        DryRunSink(confirmed=False), current_operational_revision="rev-1"
    )
    assert len(receipts) == 1
    assert restarted.get(entry.entry_id).status == "uncertain"
    restarted.reconcile(entry.entry_id, DispatchReceipt("confirmed-1", True, "rev-2", "verified"))
    assert restarted.get(entry.entry_id).status == "sent"
