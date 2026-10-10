"""Tool handlers (COMP-118): validated input model in, typed output model out.

Each handler calls `Workspace`, `DecisionTools`, `PlanningService` or the signed artifact
store in process. Handlers are synchronous; the guard runs them on a worker thread with a
timeout, maps their exceptions to stable codes and audits the call.

Idempotency keys are scoped to the caller (`claim_key(caller_id, request_key)`), so two
callers never replay each other's results.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from twinflow.application import scenarios
from twinflow.application._planning_claims import claim_key, input_digest
from twinflow.application.artifacts import ArtifactKind, ArtifactRef, SignedArtifactStore
from twinflow.application.planning import (
    CapabilityUnavailable,
    CompareResult,
    IdempotencyConflict,
    MenuRecord,
    NotFound,
    PlanningError,
    PlanPage,
    PortfolioRecord,
    RushRecord,
    SimulateResult,
)
from twinflow.application.planning_artifacts import NoPlanningProfile
from twinflow.application.portfolio_port import load_k_table_model
from twinflow.erp.build import BUILDER_VERSION
from twinflow.erp.digest import parse_snapshot
from twinflow.erp.issues import ErpIssue
from twinflow.erp.profile import PlanningProfile, load_planning_profile, profile_digest
from twinflow.erp.schema import ERP_SCHEMA_VERSION, snapshot_json_schema
from twinflow.mcp import models as m
from twinflow.mcp.spec import ToolServices
from twinflow.service.auth import Caller

MAX_REPORTED_ISSUES = 500


# --- discovery and reads ---------------------------------------------------------------------


def erp_schema(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ErpSchemaOutput:
    """The canonical ERP snapshot JSON Schema."""
    return m.ErpSchemaOutput(schema_version=ERP_SCHEMA_VERSION, json_schema=snapshot_json_schema())


def scenario_schema(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ScenarioSchemaOutput:
    """The portable scenario capsule JSON Schema."""
    return m.ScenarioSchemaOutput(json_schema=scenarios.capsule_schema())


def decision_tool_contracts(
    services: ToolServices, caller: Caller, _: m.NoInput
) -> m.ContractsOutput:
    """Event, scheduling and upload examples, units, topics and limits."""
    return m.ContractsOutput.model_validate(services.decisions.contracts())


def list_examples(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ExamplesOutput:
    """The included runnable examples."""
    rows = [m.ExampleRow.model_validate(row) for row in services.workspace.examples()]
    return m.ExamplesOutput(examples=rows)


def list_scenarios(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ScenariosOutput:
    """Saved scenario versions."""
    records = services.workspace.repository.list("scenarios")
    return m.ScenariosOutput(scenarios=[m.ScenarioOutput.model_validate(r) for r in records])


def export_scenario(
    services: ToolServices, caller: Caller, data: m.ScenarioIdInput
) -> m.ExportScenarioOutput:
    """A scenario's complete portable capsule."""
    record = services.workspace.repository.get("scenarios", data.scenario_id)
    return m.ExportScenarioOutput(scenario_id=data.scenario_id, capsule=record["capsule"])


def list_building_briefs(services: ToolServices, caller: Caller, _: m.NoInput) -> m.BriefsOutput:
    """Saved building briefs."""
    records = services.workspace.repository.list("drafts")
    return m.BriefsOutput(briefs=[m.BriefOutput.model_validate(r) for r in records])


def get_job(services: ToolServices, caller: Caller, data: m.JobIdInput) -> m.JobOutput:
    """One experiment's state and, when completed, its results."""
    return m.JobOutput.model_validate(services.workspace.repository.get("jobs", data.job_id))


def get_plan(services: ToolServices, caller: Caller, data: m.GetPlanInput) -> PlanPage:
    """One page of a stored plan's assignments."""
    return services.planning.get_plan(
        data.plan_id, offset=data.offset, limit=data.limit, job_id=data.job_id
    )


def query_evidence(
    services: ToolServices, caller: Caller, data: m.QueryEvidenceInput
) -> m.EvidenceOutput:
    """One page of decision evidence (REQ-166: offset and limit are applied)."""
    answer = services.decisions.query(
        data.job_id, data.topic, data.entity_id, offset=data.offset, limit=data.limit
    )
    return m.EvidenceOutput.model_validate(answer)


def validate_scenario(
    services: ToolServices, caller: Caller, data: m.ValidateScenarioInput
) -> m.ValidateScenarioOutput:
    """Check a capsule without saving it."""
    return m.ValidateScenarioOutput.model_validate(services.decisions.validate(data.content))


def compile_model_draft(
    services: ToolServices, caller: Caller, data: m.DraftIdInput
) -> m.CompiledDraftOutput:
    """Review a model draft's missing or unsupported facts."""
    compiled = services.workspace.compile_model_draft(data.draft_id)
    return m.CompiledDraftOutput.model_validate(compiled)


def preview_schedule_upload(
    services: ToolServices, caller: Caller, data: m.UploadPreviewInput
) -> m.UploadPreviewOutput:
    """Preview uploaded orders and every mapping issue."""
    preview = services.workspace.preview_schedule_upload(
        data.scenario_id, data.upload_name, data.content_base64, data.mapping
    )
    return m.UploadPreviewOutput.model_validate(preview)


def get_pareto_menu(services: ToolServices, caller: Caller, data: m.PortfolioIdInput) -> MenuRecord:
    """A portfolio's Pareto menu."""
    return services.planning.get_pareto_menu(data.portfolio_id)


def explain_plan(services: ToolServices, caller: Caller, data: m.ExplainInput) -> m.ExplainOutput:
    """Plan explanations (B-6): `capability_unavailable` until they ship."""
    services.planning.explain_plan(data.plan_id, data.job_ids)


def compare_plans(
    services: ToolServices, caller: Caller, data: m.ComparePlansInput
) -> CompareResult:
    """Plans of one problem side by side."""
    return services.planning.compare_plans(data.plan_ids)


def compare(services: ToolServices, caller: Caller, data: m.CompareInput) -> m.CompareOutput:
    """Two completed experiments; candidate minus baseline."""
    result = services.workspace.compare(data.baseline_id, data.candidate_id)
    return m.CompareOutput.model_validate(result)


# --- ERP snapshots and problems --------------------------------------------------------------


def validate_snapshot(
    services: ToolServices, caller: Caller, data: m.SnapshotInput
) -> m.SnapshotValidationOutput:
    """Every issue of a snapshot under the active (or named) profile; writes nothing."""
    profile, _ = services.planning_artifacts.profile(data.profile_digest)
    parsed = parse_snapshot(data.snapshot)
    if isinstance(parsed, list):
        return m.SnapshotValidationOutput(
            snapshot_digest=None, issues=tuple(parsed), hard_count=len(parsed), soft_count=0
        )
    report = services.planning.validate_snapshot(parsed, profile)
    return m.SnapshotValidationOutput.model_validate(report.model_dump())


def build_problem_from_erp(
    services: ToolServices, caller: Caller, data: m.SnapshotInput
) -> m.BuildProblemOutput:
    """Build and store the problem; a refused snapshot is a blocked build, not an error."""
    profile, provenance = services.planning_artifacts.for_build(data.profile_digest)
    parsed = parse_snapshot(data.snapshot)
    if isinstance(parsed, list):
        return _refused_build(parsed, profile)
    record = services.planning.build_problem(parsed, profile, provenance=provenance)
    return m.BuildProblemOutput.model_validate(record.model_dump())


def _refused_build(issues: list[ErpIssue], profile: PlanningProfile) -> m.BuildProblemOutput:
    return m.BuildProblemOutput(
        status="blocked",
        problem_id=None,
        problem_digest=None,
        snapshot_digest=None,
        profile_digest=profile_digest(profile),
        builder_version=BUILDER_VERSION,
        counts={},
        issues=tuple(issues[:MAX_REPORTED_ISSUES]),
        issue_total=len(issues),
    )


# --- planning (TF-003) -----------------------------------------------------------------------


def solve_portfolio(
    services: ToolServices, caller: Caller, data: m.SolvePortfolioInput
) -> PortfolioRecord:
    """Solve a stored problem with the portfolio; plans by reference."""
    return services.planning.solve_portfolio(
        data.problem_id,
        request_key=data.request_key,
        caller_id=caller.caller_id,
        time_budget_seconds=data.time_budget_seconds,
        seed=data.seed,
        pipelines=data.portfolio,
        artifacts=services.planning_artifacts,
    )


def resolve_with_overrides(
    services: ToolServices, caller: Caller, data: m.ResolveInput
) -> PortfolioRecord:
    """Re-plan from a stored plan under overrides."""
    return services.planning.resolve_with_overrides(
        data.plan_id,
        data.overrides,
        request_key=data.request_key,
        caller_id=caller.caller_id,
        time_budget_seconds=data.time_budget_seconds,
        seed=data.seed,
        artifacts=services.planning_artifacts,
    )


def insert_rush_order(
    services: ToolServices, caller: Caller, data: m.InsertRushInput
) -> RushRecord:
    """Insert a rush order into a stored plan."""
    return services.planning.insert_rush_order(
        data.plan_id,
        data.rush,
        request_key=data.request_key,
        caller_id=caller.caller_id,
        time_budget_seconds=data.time_budget_seconds,
        seed=data.seed,
        artifacts=services.planning_artifacts,
    )


def simulate_plan(services: ToolServices, caller: Caller, data: m.SimulateInput) -> SimulateResult:
    """Simulate a scenario plan; an ERP plan is blocked until the simulation link ships."""
    return services.planning.simulate_plan(
        data.plan_id,
        request_key=data.request_key,
        caller_id=caller.caller_id,
        reps=data.reps,
        seed=data.seed,
        wait_seconds=data.wait_seconds,
    )


# --- scenarios, experiments and drafts -------------------------------------------------------


def load_example(services: ToolServices, caller: Caller, data: m.ExampleInput) -> m.ScenarioOutput:
    """Import an included example as a scenario."""
    return m.ScenarioOutput.model_validate(services.workspace.load_example(data.name))


def import_scenario(
    services: ToolServices, caller: Caller, data: m.ImportScenarioInput
) -> m.ScenarioOutput:
    """Validate and import a complete capsule."""
    record = services.workspace.import_scenario(data.content, data.name)
    return m.ScenarioOutput.model_validate(record)


def branch_scenario(
    services: ToolServices, caller: Caller, data: m.BranchScenarioInput
) -> m.ScenarioOutput:
    """Import edited content as a new version of a scenario."""
    record = services.workspace.branch(data.scenario_id, data.name, data.content)
    return m.ScenarioOutput.model_validate(record)


def evaluate(services: ToolServices, caller: Caller, data: m.EvaluateInput) -> m.JobOutput:
    """Submit a bounded experiment; the key replays the same job."""
    key = claim_key(caller.caller_id, data.request_key)
    job = services.workspace.submit(data.scenario_id, data.reps, data.seed, key)
    return m.JobOutput.model_validate(job)


def cancel_job(services: ToolServices, caller: Caller, data: m.JobIdInput) -> m.JobOutput:
    """Request cancellation of an experiment."""
    return m.JobOutput.model_validate(services.workspace.cancel(data.job_id))


def save_building_brief(
    services: ToolServices, caller: Caller, data: m.BriefInput
) -> m.BriefOutput:
    """Retain supplied facts and list the missing questions."""
    draft = services.workspace.save_draft(data.name, dict(data.facts), [])
    return m.BriefOutput.model_validate(draft)


def answer_building_brief(
    services: ToolServices, caller: Caller, data: m.AnswerBriefInput
) -> m.BriefOutput:
    """Retain a new brief revision from explicit answers."""
    answers = [answer.model_dump() for answer in data.answers]
    return m.BriefOutput.model_validate(services.workspace.answer_draft(data.draft_id, answers))


def save_model_draft(
    services: ToolServices, caller: Caller, data: m.SaveModelDraftInput
) -> m.ModelDraftOutput:
    """Save typed linear-flow facts as a draft revision."""
    record = services.workspace.save_model_draft(data.name, data.draft)
    return m.ModelDraftOutput.model_validate(record)


def import_model_draft(
    services: ToolServices, caller: Caller, data: m.DraftIdInput
) -> m.ScenarioOutput:
    """Import a compiled draft as a scenario."""
    return m.ScenarioOutput.model_validate(services.workspace.import_model_draft(data.draft_id))


# --- schedules -------------------------------------------------------------------------------


def commit_schedule_upload(
    services: ToolServices, caller: Caller, data: m.UploadCommitInput
) -> m.ScheduleSnapshotOutput:
    """Commit reviewed orders as an immutable schedule snapshot."""
    record = services.workspace.commit_schedule_upload(
        data.scenario_id,
        data.upload_name,
        data.content_base64,
        data.original_file_digest,
        data.mapping,
    )
    return m.ScheduleSnapshotOutput.model_validate(record)


def solve_schedule_snapshot(
    services: ToolServices, caller: Caller, data: m.SolveScheduleSnapshotInput
) -> m.SolveSnapshotOutput:
    """Translate, solve and verify a schedule snapshot; the key replays the same plan."""
    inputs = data.model_dump(mode="json", exclude={"request_key"})

    def run() -> dict[str, Any]:
        return services.workspace.solve_schedule_snapshot(
            data.snapshot_id, data.backend, data.time_limit
        )

    claim = _Claim("solve_schedule_snapshot", "schedule_plans", data.request_key, inputs)
    record = _claimed(services, caller, claim, run)
    return m.SolveSnapshotOutput.model_validate(record)


def simulate_schedule_baseline(
    services: ToolServices, caller: Caller, data: m.ScheduleBaselineInput
) -> m.JobOutput:
    """Run normal dispatch on the uploaded orders; the key replays the same job."""
    key = claim_key(caller.caller_id, data.request_key)
    job = services.workspace.submit_schedule_baseline(data.snapshot_id, data.reps, data.seed, key)
    return m.JobOutput.model_validate(job)


def schedule_operations(
    services: ToolServices, caller: Caller, data: m.ScheduleOperationsInput
) -> m.ScheduleOutput:
    """Solve and verify a v2 SolveRequest; the key replays the same record."""
    inputs = data.model_dump(mode="json", exclude={"request_key"})

    def run() -> dict[str, Any]:
        return services.decisions.schedule(data.request, data.backend, data.time_limit)

    claim = _Claim("schedule_operations", "schedules", data.request_key, inputs)
    record = _claimed(services, caller, claim, run)
    return m.ScheduleOutput.model_validate(record)


@dataclass(frozen=True)
class _Claim:
    """One idempotent call: the method, the record kind it stores, its key and inputs."""

    method: str
    kind: str
    request_key: str
    inputs: dict[str, Any]


def _claimed(
    services: ToolServices,
    caller: Caller,
    claim: _Claim,
    run: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Replay the record claimed under the caller's key, or run and claim a new one.

    `run` stores its own record; the claim follows it. In-process duplicates of one key
    serialize on the planning service's claim lock, so one runs and the others replay it.
    A result without an `id` (a blocked translation) stores nothing and claims nothing.
    """
    repository = services.workspace.repository
    key = claim_key(caller.caller_id, claim.request_key)
    digest = input_digest(claim.method, claim.inputs)
    with services.planning.claim_lock(key):
        claimed = repository.find_request_claim(key)
        if claimed is None:
            record = run()
            if "id" not in record:
                return record
            result_id = f"{claim.kind}/{record['id']}"
            claimed = repository.claim_request(key, digest, result_id, ())
            if claimed == (digest, result_id):
                return record
    stored_digest, result_id = claimed
    if stored_digest != digest:
        raise IdempotencyConflict("The request key was already used with different inputs")
    kind, _, record_id = result_id.partition("/")
    return repository.get(kind, record_id)


# --- operational data and actions ------------------------------------------------------------


def ingest_operational_events(
    services: ToolServices, caller: Caller, data: m.IngestEventsInput
) -> m.IngestOutput:
    """Import normalized source events idempotently."""
    counts = services.decisions.ingest([dict(record) for record in data.records])
    return m.IngestOutput.model_validate(counts)


def build_operational_snapshot(
    services: ToolServices, caller: Caller, data: m.BuildOperationalSnapshotInput
) -> m.OperationalSnapshotOutput:
    """Reconcile facts known at an aware timestamp."""
    rules = [dict(rule) for rule in data.freshness_rules]
    document = services.decisions.snapshot(data.known_at, rules)
    return m.OperationalSnapshotOutput.model_validate(document)


def propose_action(
    services: ToolServices, caller: Caller, data: m.ProposeActionInput
) -> m.ProposalOutput:
    """An evidence-bound dry-run proposal; never authorizes delivery."""
    if services.actions is None:
        raise CapabilityUnavailable("Action proposals are not configured")
    record = services.actions.propose(data.job_id, dict(data.action))
    return m.ProposalOutput.model_validate(record)


# --- signed artifacts ------------------------------------------------------------------------


def get_planning_profile(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ArtifactOutput:
    """The verified active planning profile."""
    store = _store(services, NoPlanningProfile("No signed planning profile store is configured"))
    active = store.get_active("planning_profiles", PlanningProfile)
    if active is None:
        raise NoPlanningProfile("No planning profile is active; put one first")
    return _artifact_output(*active)


def put_planning_profile(
    services: ToolServices, caller: Caller, data: m.PutPlanningProfileInput
) -> ArtifactRef:
    """Validate, sign with the service key and activate a planning profile (REQ-185)."""
    store = _store(services, CapabilityUnavailable("No service signing key is configured"))
    profile = data.profile
    if profile is None:
        profile = load_planning_profile(data.profile_toml or "")
    return store.put("planning_profiles", profile, caller_id=caller.caller_id)


def get_k_table(services: ToolServices, caller: Caller, _: m.NoInput) -> m.ArtifactOutput:
    """The verified active K-table (TF-003)."""
    model = _ktable_model()
    store = _store(services, CapabilityUnavailable("No service signing key is configured"))
    active = store.get_active("k_tables", model)
    if active is None:
        raise NotFound("No K-table is active")
    return _artifact_output(*active)


def put_k_table(services: ToolServices, caller: Caller, data: m.PutKTableInput) -> ArtifactRef:
    """Validate with TF-003's `KTable`, sign and activate a K-table."""
    table = _ktable_model().model_validate(data.table)
    store = _store(services, CapabilityUnavailable("No service signing key is configured"))
    return store.put("k_tables", table, caller_id=caller.caller_id)


def _store(services: ToolServices, missing: PlanningError) -> SignedArtifactStore:
    if services.artifacts is None:
        raise missing
    return services.artifacts


def _artifact_output(artifact: BaseModel, ref: ArtifactRef) -> m.ArtifactOutput:
    kind: ArtifactKind = ref.kind
    return m.ArtifactOutput(
        kind=kind,
        digest=ref.digest,
        sequence=ref.sequence,
        key_id=ref.key_id,
        signed_at=ref.signed_at,
        artifact=artifact.model_dump(mode="json"),
    )


def _ktable_model() -> type[BaseModel]:
    """TF-003's `KTable`; absent TF-003 is `capability_unavailable`."""
    model = load_k_table_model()
    if model is None:
        raise CapabilityUnavailable("K-tables need TF-003, which is not installed")
    return model
