"""Typed input and output models of the MCP tool catalogue (COMP-118, REQ-164).

Inputs are closed (`extra="forbid"`) and bounded like the REST routes they mirror. No input
names a URL, endpoint or filesystem path (REQ-174); upload tools take `upload_name`, whose
extension alone picks the decoder (DEV-6). Outputs whose shape the workspace owns (scenario,
job and draft records) type their stable fields and allow the rest, so the published
`outputSchema` stays true as records gain fields. Planning results reuse the planning
service's own models.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from twinflow.application.artifacts import ArtifactKind
from twinflow.application.linear_draft import LinearDraft
from twinflow.application.solver_port import SolverDescription
from twinflow.erp.build import RushOrder
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import PlanningProfile
from twinflow.scheduling import Override
from twinflow.scheduling.model import MAX_OVERRIDES

JsonScalar = str | int | float | bool | None
Topic = Literal[
    "metrics", "dates", "blockers", "waits", "termination", "assumptions", "process", "schedule"
]

Ident = Annotated[str, Field(min_length=1, max_length=200)]
RecordRef = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^/\\]+$")]
"""An id or key of the simulation tools: no `/` or `\\`, so never a URL or a path (D-109)."""
_MAX_CONTENT = 5_242_880
_MAX_SEED = 4_294_967_295
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class ToolInput(BaseModel):
    """Base of every tool input: closed and immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolOutput(BaseModel):
    """Base of every closed tool output."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenOutput(BaseModel):
    """A workspace-owned record: stable fields typed, further fields allowed."""

    model_config = ConfigDict(extra="allow", frozen=True)


# --- inputs ------------------------------------------------------------------------------------


class NoInput(ToolInput):
    """A tool that takes no arguments."""


class ExampleInput(ToolInput):
    name: Ident


class ScenarioIdInput(ToolInput):
    scenario_id: Ident


class ImportScenarioInput(ToolInput):
    name: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=_MAX_CONTENT)


class BranchScenarioInput(ImportScenarioInput):
    scenario_id: Ident


class EvaluateInput(ToolInput):
    scenario_id: Ident
    request_key: Ident
    reps: int = Field(default=10, ge=1, le=50)
    seed: int = Field(default=42, ge=0, le=_MAX_SEED)


class JobIdInput(ToolInput):
    job_id: Ident


class CompareInput(ToolInput):
    baseline_id: Ident
    candidate_id: Ident


class BriefInput(ToolInput):
    name: str = Field(min_length=1, max_length=160)
    facts: dict[str, str] = Field(default_factory=dict, max_length=1000)


class AnswerItem(ToolInput):
    id: Ident
    answer: str = Field(min_length=1, max_length=10_000)


class AnswerBriefInput(ToolInput):
    draft_id: Ident
    answers: tuple[AnswerItem, ...] = Field(min_length=1, max_length=100)


class SaveModelDraftInput(ToolInput):
    name: str = Field(min_length=1, max_length=160)
    draft: LinearDraft


class DraftIdInput(ToolInput):
    draft_id: Ident


class UploadPreviewInput(ToolInput):
    scenario_id: Ident
    upload_name: str = Field(min_length=1, max_length=160)
    content_base64: str = Field(min_length=1, max_length=1_400_000)
    mapping: dict[str, str] | None = None


class UploadCommitInput(ToolInput):
    scenario_id: Ident
    upload_name: str = Field(min_length=1, max_length=160)
    content_base64: str = Field(min_length=1, max_length=1_400_000)
    original_file_digest: str = Field(pattern=_DIGEST_PATTERN)
    mapping: dict[str, str]


class SolveScheduleSnapshotInput(ToolInput):
    snapshot_id: Ident
    request_key: Ident
    backend: Ident = "baseline"  # any registered solver name (REQ-088)
    time_limit: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)


class ScheduleBaselineInput(ToolInput):
    snapshot_id: Ident
    request_key: Ident
    reps: int = Field(default=10, ge=1, le=10)
    seed: int = Field(default=42, ge=0, le=_MAX_SEED)


class ValidateScenarioInput(ToolInput):
    content: str = Field(min_length=1, max_length=_MAX_CONTENT)


class IngestEventsInput(ToolInput):
    records: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=1000)


class BuildOperationalSnapshotInput(ToolInput):
    known_at: str = Field(min_length=1, max_length=64)
    freshness_rules: tuple[dict[str, Any], ...] = Field(default=(), max_length=100)


class QueryEvidenceInput(ToolInput):
    """Exactly one of `job_id` (an experiment) and `simulation_id` (a plan simulation).

    TF-004 D-106: topic `schedule` reads a stored simulation (`simulation_id`); every
    other topic reads a completed experiment (`job_id`).
    """

    job_id: Ident | None = None
    simulation_id: RecordRef | None = None
    topic: Topic
    entity_id: str | None = Field(default=None, max_length=256)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def _exactly_one_id(self) -> QueryEvidenceInput:
        if (self.job_id is None) == (self.simulation_id is None):
            raise ValueError("give exactly one of job_id or simulation_id")
        return self


class ScheduleOperationsInput(ToolInput):
    """`request` is a v2 `SolveRequest`, parsed by the scheduling validator (REQ-083)."""

    request: dict[str, Any]
    request_key: Ident
    backend: Ident = "baseline"  # any registered solver name (REQ-088)
    time_limit: float = Field(default=10, gt=0, le=30, allow_inf_nan=False)


class ProposeActionInput(ToolInput):
    job_id: str = Field(min_length=1, max_length=256)
    action: dict[str, JsonScalar]


class SnapshotInput(ToolInput):
    """An ERP snapshot as JSON (see `erp_schema`), checked by the snapshot gate."""

    snapshot: dict[str, Any]
    profile_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class SolvePortfolioInput(ToolInput):
    problem_id: Ident
    request_key: Ident
    time_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    portfolio: tuple[str, ...] | None = Field(default=None, max_length=50)
    seed: int = Field(default=0, ge=0, le=_MAX_SEED)


class ResolveInput(ToolInput):
    plan_id: Ident
    overrides: tuple[Override, ...] = Field(max_length=MAX_OVERRIDES)
    request_key: Ident
    time_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    seed: int = Field(default=0, ge=0, le=_MAX_SEED)


class InsertRushInput(ToolInput):
    plan_id: Ident
    rush: RushOrder
    request_key: Ident
    time_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    seed: int = Field(default=0, ge=0, le=_MAX_SEED)


class GetPlanInput(ToolInput):
    plan_id: Ident
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)
    job_id: str | None = Field(default=None, max_length=200)


class PortfolioIdInput(ToolInput):
    portfolio_id: Ident


class ExplainInput(ToolInput):
    plan_id: Ident
    job_ids: tuple[str, ...] | None = Field(default=None, max_length=200)


class ComparePlansInput(ToolInput):
    """`simulation_ids`, when given, names one stored simulation per plan, same order."""

    plan_ids: tuple[str, ...] = Field(min_length=2, max_length=10)
    simulation_ids: tuple[RecordRef, ...] | None = Field(default=None, max_length=10)

    @model_validator(mode="after")
    def _one_simulation_per_plan(self) -> ComparePlansInput:
        if self.simulation_ids is not None and len(self.simulation_ids) != len(self.plan_ids):
            raise ValueError("simulation_ids needs one simulation per plan id")
        return self


class _SimulateSettings(ToolInput):
    """The settings both simulate tools share (TF-004 Data Model "Tool inputs", D-105).

    The call is synchronous (no `wait_seconds`); `reps` above the service cap is refused
    by the planning service, and the budget is clamped to the service cap.
    """

    request_key: RecordRef
    reps: int | None = Field(default=None, ge=1, le=1000)
    seed: int = Field(default=42, ge=0, le=_MAX_SEED)
    time_budget_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    profile_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


class SimulateInput(_SimulateSettings):
    plan_id: RecordRef
    baseline: bool = False


class SimulatePortfolioInput(_SimulateSettings):
    portfolio_id: RecordRef
    baseline: bool = True


class PutPlanningProfileInput(ToolInput):
    """Exactly one of `profile_toml` (TOML text) or `profile` (the typed profile)."""

    profile_toml: str | None = Field(default=None, min_length=1, max_length=1_048_576)
    profile: PlanningProfile | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> PutPlanningProfileInput:
        if (self.profile_toml is None) == (self.profile is None):
            raise ValueError("give exactly one of profile_toml or profile")
        return self


class PutKTableInput(ToolInput):
    """A TF-003 K-table; validated by `KTable` when TF-003 is installed."""

    table: dict[str, Any]


# --- outputs -----------------------------------------------------------------------------------


class CapabilitiesOutput(OpenOutput):
    schema_version: str
    mode: str
    actions: list[str]
    limits: dict[str, int]
    max_solve_seconds: float
    max_simulate_seconds: float
    max_simulation_replications: int
    max_simulation_candidates: int
    max_simulation_workers: int
    simulation_sources: list[str]
    baseline_rules: list[str]
    tool_timeouts: dict[str, float]
    portfolio_available: bool
    signing_public_key: str | None
    signing_key_id: str | None
    solvers: list[SolverDescription]


class ErpSchemaOutput(ToolOutput):
    schema_version: str
    json_schema: dict[str, Any]


class ScenarioSchemaOutput(ToolOutput):
    json_schema: dict[str, Any]


class ContractsOutput(OpenOutput):
    schema_version: str
    scheduling: dict[str, Any]
    query_topics: list[str]


class ExampleRow(ToolOutput):
    id: str
    name: str
    domain: str


class ExamplesOutput(ToolOutput):
    examples: list[ExampleRow]


class ScenarioOutput(OpenOutput):
    id: str
    name: str
    digest: str
    parent_id: str | None
    created_at: str
    capsule: dict[str, Any]
    summary: dict[str, Any]
    validity: str


class ScenariosOutput(ToolOutput):
    scenarios: list[ScenarioOutput]


class ExportScenarioOutput(ToolOutput):
    scenario_id: str
    capsule: dict[str, Any]


class JobOutput(OpenOutput):
    id: str
    scenario_id: str
    status: str
    created_at: str
    reps: int
    seed: int
    result: dict[str, Any] | None
    error: str | None


class CompareOutput(ToolOutput):
    baseline_id: str
    candidate_id: str
    compared: dict[str, Any] | None
    delta: dict[str, float]
    interpretation: str


class BriefOutput(OpenOutput):
    id: str
    name: str
    facts: dict[str, Any]
    answers: list[dict[str, str]]
    questions: list[dict[str, str]]
    status: str


class BriefsOutput(ToolOutput):
    briefs: list[BriefOutput]


class ModelDraftOutput(OpenOutput):
    id: str
    revision: int
    name: str
    draft: dict[str, Any]
    status: str


class CompiledDraftOutput(OpenOutput):
    draft_id: str
    status: str
    issues: list[dict[str, Any]]
    compiler_version: str


class UploadPreviewOutput(OpenOutput):
    scenario_id: str
    original_file_digest: str
    row_count: int
    issues: list[dict[str, Any]]
    committable: bool


class ScheduleSnapshotOutput(OpenOutput):
    id: str
    scenario_id: str
    original_file_digest: str
    orders: list[dict[str, Any]]


class SolveSnapshotOutput(OpenOutput):
    """A plan record (which carries no `status`), or `status: blocked` with the issues."""

    status: str | None = None


class ScheduleOutput(OpenOutput):
    id: str
    backend: str
    result: dict[str, Any]


class ValidateScenarioOutput(OpenOutput):
    valid: bool
    issues: list[dict[str, Any]]


class IngestOutput(ToolOutput):
    inserted: int
    duplicates: int


class OperationalSnapshotOutput(OpenOutput):
    snapshot_id: str
    known_at: str


class EvidenceOutput(OpenOutput):
    """An evidence page; `job_id` or `simulation_id` names its source (the other is None)."""

    job_id: str | None = None
    simulation_id: str | None = None
    topic: str
    evidence: list[Any]
    total: int
    offset: int
    next_offset: int | None
    validity: str


class ProposalOutput(OpenOutput):
    id: str
    mode: str
    proposal: dict[str, Any]


class SnapshotValidationOutput(ToolOutput):
    """The snapshot gate and validator report; a refused snapshot has no digest."""

    snapshot_digest: str | None
    issues: tuple[ErpIssue, ...]
    hard_count: int
    soft_count: int
    validity: Literal["provisional"] = "provisional"
    evidence_kind: Literal["erp_validation"] = "erp_validation"


class BuildProblemOutput(ToolOutput):
    """`build_problem_from_erp`: ids of the stored records plus the build report."""

    status: Literal["ready", "blocked"]
    problem_id: str | None
    problem_digest: str | None
    snapshot_digest: str | None
    profile_digest: str
    builder_version: str
    counts: dict[str, int]
    issues: tuple[ErpIssue, ...]
    issue_total: int
    validity: Literal["provisional"] = "provisional"
    evidence_kind: Literal["erp_build"] = "erp_build"


class ExplainOutput(ToolOutput):
    plan_id: str
    explanations: list[dict[str, Any]]


class ArtifactOutput(ToolOutput):
    """A verified active (or named) signed artifact."""

    kind: ArtifactKind
    digest: str
    sequence: int
    key_id: str
    signed_at: datetime
    artifact: dict[str, Any]
