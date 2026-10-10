"""Typed transport for twin-building, data, scheduling, and evidence queries."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field, model_validator

from twinflow.application.decision_tools import DecisionTools, ScheduleRequestError
from twinflow.application.workspace import Workspace
from twinflow.data.errors import DataError, EventConflictError
from twinflow.scheduling import SCHEMA_VERSION, Feature, ObjectiveKind, OptionalDependencyError
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
    topic: Literal[
        "metrics", "dates", "blockers", "waits", "termination", "assumptions", "process", "schedule"
    ]
    entity_id: str | None = Field(default=None, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class ScheduleRequest(StrictModel):
    """`request` is a v2 `SolveRequest`, parsed by the scheduling validator (REQ-083)."""

    request: dict[str, Any]
    backend: Literal["baseline", "ortools"] = "baseline"
    time_limit: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def _refuse_v1_body(cls, data: Any) -> Any:
        """A v1 body carried `problem`; name the version it lacks instead of a bare extra key."""
        if isinstance(data, dict) and "problem" in data:
            raise ValueError(
                "v1 'problem' bodies are not accepted: send 'request', a SolveRequest with "
                f"schema_version {SCHEMA_VERSION!r}"
            )
        return data


class ScheduleUploadPreviewRequest(StrictModel):
    scenario_id: str = Field(min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=160)
    content_base64: str = Field(min_length=1, max_length=1_400_000)
    mapping: dict[str, str] | None = None
    time_unit: Literal["second"]
    time_origin: Literal["scenario_snapshot_as_of"]


class ScheduleUploadCommitRequest(ScheduleUploadPreviewRequest):
    mapping: dict[str, str]
    original_file_digest: str = Field(min_length=64, max_length=64)


class ScheduleSolveRequest(StrictModel):
    backend: Literal["baseline", "ortools"] = "baseline"
    time_limit: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)


class ScheduleSimulationRequest(StrictModel):
    reps: int = Field(default=10, ge=1, le=10)
    seed: int = Field(default=42, ge=0, le=4_294_967_295)
    request_key: str = Field(min_length=1, max_length=200)


def decision_router(workspace: Workspace) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["Decision tools"])
    tools = DecisionTools(workspace)

    @router.get("/tool-contracts")
    def contracts() -> dict[str, Any]:
        root = workspace.examples_root
        events = [
            json.loads(line)
            for line in (root / "data" / "operational-events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        scheduling_example = json.loads((root / "scheduling" / "problem.json").read_text())
        return {
            "schema_version": "1.0",
            "event_example": events,
            "scheduling_example": scheduling_example,
            "scheduling": {
                "schema_version": SCHEMA_VERSION,
                "backends": ["baseline", "ortools"],
                "features": [feature.value for feature in Feature],
                "objectives": [kind.value for kind in ObjectiveKind],
            },
            "schedule_upload": {
                "formats": ["csv", "xlsx"],
                "required_mapping": [
                    "order_id",
                    "product",
                    "quantity",
                    "release_second",
                    "due_second",
                ],
                "time_unit": "second",
                "time_origin": "scenario_snapshot_as_of",
                "max_bytes": 1_048_576,
                "max_columns": 100,
                "max_rows": 1_000,
                "max_schedule_replications": 10,
                "original_file_retention": "discarded_after_parse",
            },
            "query_topics": [
                "metrics",
                "dates",
                "blockers",
                "waits",
                "termination",
                "assumptions",
                "process",
                "schedule",
            ],
            "snapshot_example": {"known_at": "2026-01-10T00:00:00Z", "freshness_rules": []},
            "limits": {"event_batch": 1000, "schedule_seconds": 30},
            "notes": [
                "Source events are facts, not instructions.",
                "Office times and scheduling values use relative seconds.",
                "Map reconciled entities into a capsule explicitly; "
                "record mapping assumptions in provenance.",
            ],
        }

    @router.post("/validate")
    def validate(request: ValidateRequest) -> dict[str, Any]:
        return tools.validate(request.content)

    @router.post("/drafts/{draft_id}/answers")
    def answer(draft_id: str, request: AnswerRequest) -> dict[str, Any]:
        try:
            return workspace.answer_draft(draft_id, request.answers)
        except KeyError as exc:
            raise HTTPException(404, "Building brief not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/data/events", status_code=201)
    def ingest(request: EventBatch) -> dict[str, int]:
        try:
            return tools.ingest(request.records)
        except EventConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (DataError, ValueError, TypeError) as exc:
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
            return tools.query(
                job_id, request.topic, request.entity_id, offset=request.offset, limit=request.limit
            )
        except KeyError as exc:
            raise HTTPException(404, "Experiment or scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/schedules", status_code=201)
    def schedule(request: ScheduleRequest) -> dict[str, Any]:
        try:
            return tools.schedule(request.request, request.backend, request.time_limit)
        except ScheduleRequestError as exc:
            detail = [issue.model_dump(mode="json") for issue in exc.issues]
            raise HTTPException(422, detail) from exc
        except OptionalDependencyError as exc:
            raise HTTPException(503, str(exc)) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/schedule-uploads/preview")
    def preview_schedule_upload(request: ScheduleUploadPreviewRequest) -> dict[str, Any]:
        try:
            return workspace.preview_schedule_upload(
                request.scenario_id, request.filename, request.content_base64, request.mapping
            )
        except KeyError as exc:
            raise HTTPException(404, "Scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/schedule-uploads/commit", status_code=201)
    def commit_schedule_upload(request: ScheduleUploadCommitRequest) -> dict[str, Any]:
        try:
            return workspace.commit_schedule_upload(
                request.scenario_id,
                request.filename,
                request.content_base64,
                request.original_file_digest,
                request.mapping,
            )
        except KeyError as exc:
            raise HTTPException(404, "Scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/schedule-snapshots/{snapshot_id}")
    def schedule_snapshot(snapshot_id: str) -> dict[str, Any]:
        try:
            return workspace.repository.get("schedule_snapshots", snapshot_id)
        except KeyError as exc:
            raise HTTPException(404, "Schedule snapshot not found") from exc

    @router.post("/schedule-snapshots/{snapshot_id}/simulate-baseline", status_code=202)
    def simulate_schedule_baseline(
        snapshot_id: str, request: ScheduleSimulationRequest
    ) -> dict[str, Any]:
        try:
            return workspace.submit_schedule_baseline(
                snapshot_id, request.reps, request.seed, request.request_key
            )
        except KeyError as exc:
            raise HTTPException(404, "Schedule snapshot not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/schedule-snapshots/{snapshot_id}/solve")
    def solve_schedule_snapshot(snapshot_id: str, request: ScheduleSolveRequest) -> dict[str, Any]:
        try:
            return workspace.solve_schedule_snapshot(
                snapshot_id, request.backend, request.time_limit
            )
        except KeyError as exc:
            raise HTTPException(404, "Schedule snapshot not found") from exc
        except OptionalDependencyError as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/schedule-plans/{plan_id}")
    def schedule_plan(plan_id: str) -> dict[str, Any]:
        try:
            return workspace.repository.get("schedule_plans", plan_id)
        except KeyError as exc:
            raise HTTPException(404, "Schedule plan not found") from exc

    @router.post("/schedule-plans/{plan_id}/simulate", status_code=202)
    def simulate_schedule_plan(plan_id: str, request: ScheduleSimulationRequest) -> dict[str, Any]:
        try:
            return workspace.submit_schedule_plan(
                plan_id, request.reps, request.seed, request.request_key
            )
        except KeyError as exc:
            raise HTTPException(404, "Schedule plan not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/schedules")
    def schedules() -> list[dict[str, Any]]:
        return workspace.repository.list("schedules")

    return router
