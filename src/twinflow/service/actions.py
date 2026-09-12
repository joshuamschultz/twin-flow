"""Separate operator review and agent proposal surfaces; delivery is always dry-run."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import Field

from twinflow.actions import JsonScalar
from twinflow.application.action_tools import ActionTools
from twinflow.application.workspace import Workspace
from twinflow.service.workspace import StrictModel


class ProposalRequest(StrictModel):
    job_id: str = Field(min_length=1, max_length=256)
    action: dict[str, JsonScalar]


class ApprovalRequest(StrictModel):
    reviewed_digest: str = Field(min_length=1, max_length=100)
    reviewer: str = Field(min_length=1, max_length=160)


class DeliverRequest(StrictModel):
    approval_id: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=200)
    current_revision: str = Field(min_length=1, max_length=256)


def action_router(workspace: Workspace) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["Dry-run action review"])
    tools = ActionTools(workspace)

    @router.get("/proposals")
    def proposals() -> list[dict[str, Any]]:
        return workspace.repository.list("proposals")

    @router.post("/proposals", status_code=201)
    def propose(request: ProposalRequest) -> dict[str, Any]:
        try:
            return tools.propose(request.job_id, request.action)
        except KeyError as exc:
            raise HTTPException(404, "Experiment not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/proposals/{proposal_id}/approve", status_code=201)
    def approve(proposal_id: str, request: ApprovalRequest) -> dict[str, Any]:
        try:
            return tools.approve(proposal_id, request.reviewed_digest, request.reviewer)
        except KeyError as exc:
            raise HTTPException(404, "Proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/proposals/{proposal_id}/dry-run")
    def deliver(proposal_id: str, request: DeliverRequest) -> dict[str, Any]:
        try:
            return tools.deliver(
                proposal_id, request.approval_id, request.request_key, request.current_revision
            )
        except KeyError as exc:
            raise HTTPException(404, "Proposal not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
