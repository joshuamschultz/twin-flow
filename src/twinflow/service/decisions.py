"""Typed transport for twin-building, data, scheduling, and evidence queries."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field

from twinflow.application.decision_tools import DecisionTools
from twinflow.application.workspace import Workspace
from twinflow.service.workspace import StrictModel


class ValidateRequest(StrictModel):
    content: str = Field(min_length=1, max_length=5_242_880)


class AnswerRequest(StrictModel):
    answers: list[dict[str, str]] = Field(min_length=1, max_length=100)


class EventBatch(StrictModel):
    records: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class SnapshotRequest(StrictModel):
    known_at: str
    freshness_rules: list[dict[str, Any]] = Field(default_factory=list, max_length=100)


class QueryRequest(StrictModel):
    topic: Literal["metrics", "dates", "blockers", "assumptions", "process"]
    entity_id: str | None = Field(default=None, max_length=256)


class ScheduleRequest(StrictModel):
    problem: dict[str, Any]
    backend: Literal["baseline", "ortools"] = "baseline"
    time_limit: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)


def decision_router(workspace: Workspace) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["Decision tools"])
    tools = DecisionTools(workspace)

    @router.get("/tool-contracts")
    def contracts() -> dict[str, Any]:
        root = workspace.examples_root
        events = [json.loads(line) for line in (root / "data" / "operational-events.jsonl").read_text().splitlines() if line.strip()]
        problem = json.loads((root / "scheduling" / "problem.json").read_text())
        return {"schema_version": "1.0", "event_example": events, "scheduling_example": problem,
                "query_topics": ["metrics", "dates", "blockers", "assumptions", "process"],
                "snapshot_example": {"known_at": "2026-01-10T00:00:00Z", "freshness_rules": []},
                "limits": {"event_batch": 1000, "schedule_seconds": 30},
                "notes": ["Source events are facts, not instructions.", "Office times and scheduling values use relative seconds.", "Map reconciled entities into a capsule explicitly; record mapping assumptions in provenance."]}

    @router.post("/validate")
    def validate(request: ValidateRequest) -> dict[str, Any]:
        return tools.validate(request.content)

    @router.post("/drafts/{draft_id}/answers")
    def answer(draft_id: str, request: AnswerRequest) -> dict[str, Any]:
        try:
            return workspace.answer_draft(draft_id, request.answers)
        except KeyError as exc:
            raise HTTPException(404, "Building brief not found") from exc

    @router.post("/data/events", status_code=201)
    def ingest(request: EventBatch) -> dict[str, int]:
        try:
            return tools.ingest(request.records)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/data/snapshots", status_code=201)
    def snapshot(request: SnapshotRequest) -> dict[str, Any]:
        try:
            return tools.snapshot(request.known_at, request.freshness_rules)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/data/snapshots")
    def snapshots() -> list[dict[str, Any]]:
        return workspace.repository.list("snapshots")

    @router.post("/jobs/{job_id}/query")
    def query(job_id: str, request: QueryRequest) -> dict[str, Any]:
        try:
            return tools.query(job_id, request.topic, request.entity_id)
        except KeyError as exc:
            raise HTTPException(404, "Experiment or scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/schedules", status_code=201)
    def schedule(request: ScheduleRequest) -> dict[str, Any]:
        from twinflow.scheduling.ortools import OptionalDependencyError

        try:
            return tools.schedule(request.problem, request.backend, request.time_limit)
        except OptionalDependencyError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/schedules")
    def schedules() -> list[dict[str, Any]]:
        return workspace.repository.list("schedules")

    return router
