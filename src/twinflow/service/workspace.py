"""Typed workspace transport; all decisions live in application services."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from twinflow.application import scenarios
from twinflow.application.workspace import Workspace


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImportRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=5_242_880)


class ExampleRequest(StrictModel):
    example_id: str = Field(min_length=1, max_length=100)


class EvaluateRequest(StrictModel):
    reps: int = Field(default=10, ge=1, le=50)
    seed: int = Field(default=42, ge=0, le=4_294_967_295)
    request_key: str = Field(min_length=1, max_length=200)


class CompareRequest(StrictModel):
    baseline_id: str
    candidate_id: str


class DraftRequest(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    facts: dict[str, Any] = Field(default_factory=dict)
    answers: list[dict[str, str]] = Field(default_factory=list, max_length=100)


class ScenarioResponse(BaseModel):
    id: str
    name: str
    digest: str
    parent_id: str | None
    created_at: str
    capsule: dict[str, Any]
    summary: dict[str, Any]
    validity: str


class JobResponse(BaseModel):
    id: str
    scenario_id: str
    status: Literal[
        "queued", "running", "completed", "failed", "canceled", "cancel_requested", "interrupted"
    ]
    created_at: str
    finished_at: str | None = None
    reps: int
    seed: int
    result: dict[str, Any] | None
    error: str | None


class CapabilitiesResponse(BaseModel):
    schema_version: str
    mode: str
    actions: list[str]
    limits: dict[str, int]
    supported_profiles: list[str]
    agent_workflow: list[str]
    production_writeback: bool


class ExampleResponse(BaseModel):
    id: str
    name: str
    domain: str = "manufacturing"


class CompareResponse(BaseModel):
    baseline_id: str
    candidate_id: str
    delta: dict[str, float]
    interpretation: str


class DraftResponse(BaseModel):
    id: str
    name: str
    facts: dict[str, Any]
    answers: list[dict[str, str]]
    questions: list[dict[str, str]]
    created_at: str
    status: str
    next_step: str


def workspace_router(workspace: Workspace) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["Agent workspace"])

    @router.get("/capabilities", response_model=CapabilitiesResponse, status_code=200)
    def capabilities() -> dict[str, Any]:
        return workspace.capabilities()

    @router.get("/schema", response_class=Response, status_code=200)
    def schema() -> Response:
        return Response(
            json.dumps(scenarios.capsule_schema()), media_type="application/schema+json"
        )

    @router.get("/examples", response_model=list[ExampleResponse], status_code=200)
    def examples() -> list[dict[str, str]]:
        return workspace.examples()

    @router.post("/examples/load", response_model=ScenarioResponse, status_code=201)
    def load_example(request: ExampleRequest) -> dict[str, Any]:
        try:
            return workspace.load_example(request.example_id)
        except KeyError as exc:
            raise HTTPException(404, "Example not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/scenarios", response_model=list[ScenarioResponse], status_code=200)
    def list_scenarios() -> list[dict[str, Any]]:
        return workspace.repository.list("scenarios")

    @router.post("/scenarios", response_model=ScenarioResponse, status_code=201)
    def import_scenario(request: ImportRequest) -> dict[str, Any]:
        try:
            return workspace.import_scenario(request.content, request.name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/scenarios/{scenario_id}", response_model=ScenarioResponse, status_code=200)
    def scenario(scenario_id: str) -> dict[str, Any]:
        try:
            return workspace.repository.get("scenarios", scenario_id)
        except KeyError as exc:
            raise HTTPException(404, "Scenario not found") from exc

    @router.get("/scenarios/{scenario_id}/export", response_class=Response, status_code=200)
    def export_scenario(scenario_id: str) -> Response:
        record = scenario(scenario_id)
        return Response(
            json.dumps(record["capsule"], indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="scenario.twin.json"'},
        )

    @router.post(
        "/scenarios/{scenario_id}/branch", response_model=ScenarioResponse, status_code=201
    )
    def branch(scenario_id: str, request: ImportRequest) -> dict[str, Any]:
        try:
            return workspace.branch(scenario_id, request.name, request.content)
        except KeyError as exc:
            raise HTTPException(404, "Parent scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/scenarios/{scenario_id}/evaluate", response_model=JobResponse, status_code=202)
    def evaluate(scenario_id: str, request: EvaluateRequest) -> dict[str, Any]:
        try:
            return workspace.submit(scenario_id, request.reps, request.seed, request.request_key)
        except KeyError as exc:
            raise HTTPException(404, "Scenario not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/jobs", response_model=list[JobResponse], status_code=200)
    def list_jobs() -> list[dict[str, Any]]:
        return workspace.repository.list("jobs")

    @router.get("/jobs/{job_id}", response_model=JobResponse, status_code=200)
    def job(job_id: str) -> dict[str, Any]:
        try:
            return workspace.repository.get("jobs", job_id)
        except KeyError as exc:
            raise HTTPException(404, "Experiment not found") from exc

    @router.post("/jobs/{job_id}/cancel", response_model=JobResponse, status_code=200)
    def cancel(job_id: str) -> dict[str, Any]:
        try:
            return workspace.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "Experiment not found") from exc

    @router.get("/jobs/{job_id}/export", response_class=Response, status_code=200)
    def export_result(job_id: str) -> Response:
        record = job(job_id)
        return Response(
            json.dumps(record, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="experiment-evidence.json"'},
        )

    @router.post("/compare", response_model=CompareResponse, status_code=200)
    def compare(request: CompareRequest) -> dict[str, Any]:
        try:
            return workspace.compare(request.baseline_id, request.candidate_id)
        except KeyError as exc:
            raise HTTPException(404, "Experiment not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/drafts", response_model=DraftResponse, status_code=201)
    def draft(request: DraftRequest) -> dict[str, Any]:
        return workspace.save_draft(request.name, request.facts, request.answers)

    @router.get("/drafts", response_model=list[DraftResponse], status_code=200)
    def drafts() -> list[dict[str, Any]]:
        return workspace.repository.list("drafts")

    return router
