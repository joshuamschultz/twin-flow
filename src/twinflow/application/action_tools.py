"""Evidence-bound proposal review and dry-run delivery for one trusted workspace."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from twinflow.actions import Approval, DryRunSink, JsonScalar, Proposal, TransactionalOutbox
from twinflow.application.workspace import Workspace


class ActionTools:
    """The application fixes the sink to dry run; no arbitrary connector selection."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.outbox = TransactionalOutbox(workspace.repository.root / "actions.sqlite3")
        self.outbox.recover()

    def propose(self, job_id: str, action: dict[str, JsonScalar]) -> dict[str, Any]:
        job = self.workspace.repository.get("jobs", job_id)
        if job["status"] != "completed":
            raise ValueError("A proposal requires completed experiment evidence")
        scenario = self.workspace.repository.get("scenarios", job["scenario_id"])
        revision = str(scenario["capsule"]["snapshot"]["model_revision"])
        proposal = Proposal.create(uuid.uuid4().hex, scenario["id"], job_id, revision, action)
        record = {"id": proposal.proposal_id, "proposal": asdict(proposal), "mode": "dry_run",
                  "scenario_digest": scenario["digest"], "created_at": self.workspace.now()}
        self.workspace.repository.create("proposals", proposal.proposal_id, record)
        return record

    def approve(self, proposal_id: str, reviewed_digest: str, reviewer: str) -> dict[str, Any]:
        record = self.workspace.repository.get("proposals", proposal_id)
        proposal = Proposal(**record["proposal"])
        if reviewed_digest != proposal.digest:
            raise ValueError("The reviewed digest does not match this proposal")
        action_type = proposal.action.get("type")
        if not isinstance(action_type, str) or not action_type:
            raise ValueError("Action requires a nonempty type")
        approval = Approval(uuid.uuid4().hex, proposal.digest, reviewer,
                            frozenset({action_type}), datetime.now(UTC) + timedelta(minutes=15))
        self.outbox.register_approval(approval)
        self.workspace.repository.append_audit(self.workspace.now(), "approve_dry_run",
                                               "proposals", proposal_id, "approved")
        return {"approval_id": approval.approval_id, "proposal_digest": proposal.digest,
                "expires_at": approval.expires_at.isoformat(), "mode": "dry_run"}

    def deliver(self, proposal_id: str, approval_id: str, request_key: str,
                current_revision: str) -> dict[str, Any]:
        record = self.workspace.repository.get("proposals", proposal_id)
        proposal = Proposal(**record["proposal"])
        entry = self.outbox.submit(proposal, approval_id, request_key, now=datetime.now(UTC))
        receipts = self.outbox.dispatch_pending(DryRunSink(),
            current_operational_revision=current_revision, entry_id=entry.entry_id, now=datetime.now(UTC))
        return {"entry": asdict(self.outbox.get(entry.entry_id)),
                "receipts": [asdict(receipt) for receipt in receipts], "mode": "dry_run",
                "interpretation": "No operational system was changed. Revision supplied for this dry run."}
