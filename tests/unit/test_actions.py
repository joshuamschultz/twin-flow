from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    receipts = restarted.dispatch_pending(
        sink, current_operational_revision="rev-1", now=datetime.now(UTC)
    )

    assert duplicate.entry_id == entry.entry_id
    assert len(receipts) == 1
    assert restarted.get(entry.entry_id).status == "sent"
    assert (
        restarted.dispatch_pending(
            sink, current_operational_revision="rev-1", now=datetime.now(UTC)
        )
        == []
    )
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
    assert (
        outbox.dispatch_pending(sink, current_operational_revision="rev-2", now=datetime.now(UTC))
        == []
    )
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
        outbox.dispatch_pending(
            FailingSink(), current_operational_revision="rev-1", now=datetime.now(UTC)
        )

    restarted = TransactionalOutbox(database)
    assert restarted.get(entry.entry_id).status == "delivering"
    assert restarted.recover() == 1
    receipts = restarted.dispatch_pending(
        DryRunSink(confirmed=False), current_operational_revision="rev-1", now=datetime.now(UTC)
    )
    assert len(receipts) == 1
    assert restarted.get(entry.entry_id).status == "uncertain"
    restarted.reconcile(entry.entry_id, DispatchReceipt("confirmed-1", True, "rev-2", "verified"))
    assert restarted.get(entry.entry_id).status == "sent"


@pytest.mark.parametrize("mode", ["revoked", "expired"])
def test_dispatch_rechecks_authorization_after_submission(tmp_path: Path, mode: str) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    proposal = _proposal()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=5)
    outbox.register_approval(
        Approval("a-1", proposal.digest, "operator", frozenset({"release"}), expires)
    )
    entry = outbox.submit(proposal, "a-1", "request", now=now)
    if mode == "revoked":
        outbox.revoke("a-1")
    dispatch_time = now if mode == "revoked" else expires
    sink = DryRunSink()

    assert (
        outbox.dispatch_pending(sink, current_operational_revision="rev-1", now=dispatch_time) == []
    )
    assert outbox.get(entry.entry_id).status == "rejected"
    assert sink.calls == []


def test_targeted_dispatch_and_bounded_listing(tmp_path: Path) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    entries = []
    for index in range(2):
        proposal = Proposal.create(
            f"p-{index}", "scenario", f"result-{index}", "rev-1", {"type": "release"}
        )
        approval_id = f"a-{index}"
        outbox.register_approval(
            Approval(
                approval_id,
                proposal.digest,
                "operator",
                frozenset({"release"}),
                datetime.now(UTC) + timedelta(hours=1),
            )
        )
        entries.append(
            outbox.submit(proposal, approval_id, f"request-{index}", now=datetime.now(UTC))
        )
    sink = DryRunSink()
    receipts = outbox.dispatch_pending(
        sink,
        current_operational_revision="rev-1",
        now=datetime.now(UTC),
        entry_id=entries[1].entry_id,
    )
    assert len(receipts) == 1
    assert sink.calls == ["request-1"]
    assert outbox.get(entries[0].entry_id).status == "pending"
    assert outbox.list_entries(limit=1) == [entries[0]]
    with pytest.raises(ActionError, match="limit"):
        outbox.list_entries(limit=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [("proposal_id", ""), ("scenario_id", "x" * 257), ("action", float("nan"))],
)
def test_proposal_rejects_unbounded_or_nonfinite_content(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "proposal_id": "proposal",
        "scenario_id": "scenario",
        "result_id": "result",
        "expected_operational_revision": "revision",
        "action": {"type": "release", "qty": 1},
    }
    if field == "action":
        kwargs[field] = {"type": "release", "qty": value}
    else:
        kwargs[field] = value
    with pytest.raises(ActionError):
        Proposal.create(**kwargs)  # type: ignore[arg-type]


def test_proposal_rejects_oversized_canonical_action() -> None:
    with pytest.raises(ActionError, match="exceeds"):
        Proposal.create(
            "proposal",
            "scenario",
            "result",
            "revision",
            {"type": "release", "value": "x" * 65_536},
        )


def test_reconcile_is_idempotent_but_rejects_conflicting_receipt(tmp_path: Path) -> None:
    outbox = TransactionalOutbox(tmp_path / "actions.sqlite3")
    proposal = _proposal()
    _approve(outbox, proposal)
    entry = outbox.submit(proposal, "a-1", "request", now=datetime.now(UTC))
    outbox.dispatch_pending(
        DryRunSink(confirmed=False),
        current_operational_revision="rev-1",
        now=datetime.now(UTC),
    )
    receipts = (
        DispatchReceipt("confirmed", True, "rev-2", "verified"),
        DispatchReceipt("rejected", False, None, "different result"),
    )

    def reconcile(receipt: DispatchReceipt) -> DispatchReceipt | ActionError:
        try:
            outbox.reconcile(entry.entry_id, receipt)
            return receipt
        except ActionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reconcile, receipts))
    winners = [outcome for outcome in outcomes if isinstance(outcome, DispatchReceipt)]
    assert len(winners) == 1
    assert sum(isinstance(outcome, ActionError) for outcome in outcomes) == 1
    outbox.reconcile(entry.entry_id, winners[0])
    loser = receipts[1] if winners[0] == receipts[0] else receipts[0]
    with pytest.raises(ActionError, match="different receipt"):
        outbox.reconcile(entry.entry_id, loser)
