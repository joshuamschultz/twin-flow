"""The tool catalogue table (COMP-118): one `ToolSpec` per tool, shared by every surface.

`CATALOGUE` drives the MCP registrations (`tools.py`) and the REST planning routes
(`service/planning.py`); `SPECS` indexes it by tool name. Each row has a typed input model,
a typed output model, a classification with the scope it needs (`read_only` -> `read`,
`state_modifying` -> `plan`), MCP hints and a guard timeout.

This module imports no `mcp` SDK module, so REST serves the same specs without the
`agent` extra.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from twinflow.application import solver_port
from twinflow.application.artifacts import ArtifactRef
from twinflow.application.planning import (
    CompareResult,
    MenuRecord,
    PlanPage,
    PortfolioRecord,
    PortfolioSimulation,
    RushRecord,
    SimulateResult,
)
from twinflow.mcp import _handlers as h
from twinflow.mcp import models as m
from twinflow.mcp.spec import Classification, Handler, TimeoutCap, ToolServices, ToolSpec
from twinflow.policy import DISPATCH_NAMES
from twinflow.service.auth import Caller, required_scope

__all__ = ["CATALOGUE", "SPECS", "describe_capabilities"]

# Guard timeouts (REQ-187). Solve-type tools wait the configured planning service's solve
# cap (REQ-169), simulate tools its simulate cap (TF-004 D-105), plus the same 15 s grace
# its own wall-clock limit allows (`ToolSpec.timeout_for`).
GRACE_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 30.0
ERP_TIMEOUT_SECONDS = 60.0
SCHEDULE_TIMEOUT_SECONDS = 30.0 + GRACE_SECONDS  # schedule time_limit is capped at 30

# The distribution sources a plan simulation can draw from (TF-004 COMP-132, REQ-139).
SIMULATION_SOURCES = ("deterministic", "profile", "capsule")


def describe_capabilities(
    services: ToolServices, caller: Caller, _: m.NoInput
) -> m.CapabilitiesOutput:
    """Actions, limits, the solve and simulation caps, every tool's timeout and the signing key.

    REQ-169, REQ-186, REQ-187, TF-004 REQ-139 (simulation caps, sources and baseline
    rules). The key is the raw 32-byte Ed25519 public key in lowercase hex; both key fields
    are None when no signing key is configured.

    Result contract for schedule_operations and solve_schedule_snapshot (REST `POST
    /schedules` answers 201): a semantic refusal (cycle, unknown reference, horizon) is a
    stored result with verified=false and typed issues, not an error; only malformed input
    is refused (REST 422/413). A success status never means the plan is good: read
    `verified` and `issues`.
    """
    artifacts = services.artifacts
    return m.CapabilitiesOutput.model_validate(
        {
            **services.workspace.capabilities(),
            "max_solve_seconds": services.planning.max_solve_seconds,
            **services.planning.simulation_caps(),
            "simulation_sources": list(SIMULATION_SOURCES),
            "baseline_rules": list(DISPATCH_NAMES),
            "portfolio_available": services.planning.portfolio_available,
            "tool_timeouts": {spec.name: spec.timeout_for(services) for spec in CATALOGUE},
            "signing_public_key": artifacts.public_key_raw.hex() if artifacts else None,
            "signing_key_id": artifacts.key_id if artifacts else None,
            "solvers": solver_port.available_solvers(),
        }
    )


def _spec(
    name: str,
    models: tuple[type[BaseModel], type[BaseModel]],
    handler: Handler,
    classification: Classification,
    *,
    idempotent: bool = True,
    destructive: bool = False,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    solve_slot: bool = False,
    timeout_cap: TimeoutCap = "fixed",
    scope: str | None = None,
) -> ToolSpec:
    """One catalogue row; the description is the handler's docstring.

    `scope` overrides the classification's default scope (only the `admin` tools do).
    """
    return ToolSpec(
        name=name,
        input_model=models[0],
        output_model=models[1],
        classification=classification,
        scope=scope if scope is not None else required_scope(classification),
        idempotent=idempotent,
        destructive=destructive,
        handler=handler,
        timeout_seconds=timeout,
        description=(handler.__doc__ or name).strip(),
        solve_slot=solve_slot,
        timeout_cap=timeout_cap,
    )


def _read(
    name: str,
    models: tuple[type[BaseModel], type[BaseModel]],
    handler: Handler,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ToolSpec:
    return _spec(name, models, handler, "read_only", timeout=timeout)


def _append(
    name: str, models: tuple[type[BaseModel], type[BaseModel]], handler: Handler
) -> ToolSpec:
    """A state-modifying tool that appends a record on every call (not idempotent)."""
    return _spec(name, models, handler, "state_modifying", idempotent=False)


def _admin(
    name: str, models: tuple[type[BaseModel], type[BaseModel]], handler: Handler
) -> ToolSpec:
    """A state-modifying, non-idempotent tool that needs the `admin` scope."""
    return _spec(name, models, handler, "state_modifying", idempotent=False, scope="admin")


def _keyed(
    name: str,
    models: tuple[type[BaseModel], type[BaseModel]],
    handler: Handler,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ToolSpec:
    """A state-modifying tool that replays its result for a repeated key (idempotent)."""
    return _spec(name, models, handler, "state_modifying", timeout=timeout)


def _solve(
    name: str, models: tuple[type[BaseModel], type[BaseModel]], handler: Handler
) -> ToolSpec:
    """A keyed solve-type tool: holds a solve slot; times out at the solve cap plus grace."""
    return _spec(
        name,
        models,
        handler,
        "state_modifying",
        timeout=GRACE_SECONDS,
        solve_slot=True,
        timeout_cap="solve",
    )


def _simulate(
    name: str, models: tuple[type[BaseModel], type[BaseModel]], handler: Handler
) -> ToolSpec:
    """A keyed simulate tool: holds a solve slot; times out at the simulate cap plus grace.

    D-105: a simulation is synchronous and CPU-bound like a solve, so it shares the
    caller's solve slots, but its timeout follows `max_simulate_seconds`, not the solve cap.
    """
    return _spec(
        name,
        models,
        handler,
        "state_modifying",
        timeout=GRACE_SECONDS,
        solve_slot=True,
        timeout_cap="simulate",
    )


CATALOGUE: tuple[ToolSpec, ...] = (
    # --- read_only -------------------------------------------------------------------------
    _read("describe_capabilities", (m.NoInput, m.CapabilitiesOutput), describe_capabilities),
    _read("erp_schema", (m.NoInput, m.ErpSchemaOutput), h.erp_schema),
    _read("scenario_schema", (m.NoInput, m.ScenarioSchemaOutput), h.scenario_schema),
    _read("decision_tool_contracts", (m.NoInput, m.ContractsOutput), h.decision_tool_contracts),
    _read("list_examples", (m.NoInput, m.ExamplesOutput), h.list_examples),
    _read("list_scenarios", (m.NoInput, m.ScenariosOutput), h.list_scenarios),
    _read("export_scenario", (m.ScenarioIdInput, m.ExportScenarioOutput), h.export_scenario),
    _read("list_building_briefs", (m.NoInput, m.BriefsOutput), h.list_building_briefs),
    _read("get_job", (m.JobIdInput, m.JobOutput), h.get_job),
    _read("get_plan", (m.GetPlanInput, PlanPage), h.get_plan),
    _read("query_evidence", (m.QueryEvidenceInput, m.EvidenceOutput), h.query_evidence),
    _read(
        "validate_snapshot",
        (m.SnapshotInput, m.SnapshotValidationOutput),
        h.validate_snapshot,
        timeout=ERP_TIMEOUT_SECONDS,
    ),
    _read(
        "validate_scenario",
        (m.ValidateScenarioInput, m.ValidateScenarioOutput),
        h.validate_scenario,
    ),
    _read("compile_model_draft", (m.DraftIdInput, m.CompiledDraftOutput), h.compile_model_draft),
    _read(
        "preview_schedule_upload",
        (m.UploadPreviewInput, m.UploadPreviewOutput),
        h.preview_schedule_upload,
    ),
    _read("get_pareto_menu", (m.PortfolioIdInput, MenuRecord), h.get_pareto_menu),
    _read("explain_plan", (m.ExplainInput, m.ExplainOutput), h.explain_plan),
    _read("compare_plans", (m.ComparePlansInput, CompareResult), h.compare_plans),
    _read("compare", (m.CompareInput, m.CompareOutput), h.compare),
    _read("get_planning_profile", (m.NoInput, m.ArtifactOutput), h.get_planning_profile),
    _read("get_k_table", (m.NoInput, m.ArtifactOutput), h.get_k_table),
    # --- state_modifying, idempotent (content address or request key) ------------------------
    _keyed(
        "build_problem_from_erp",
        (m.SnapshotInput, m.BuildProblemOutput),
        h.build_problem_from_erp,
        timeout=ERP_TIMEOUT_SECONDS,
    ),
    _solve("solve_portfolio", (m.SolvePortfolioInput, PortfolioRecord), h.solve_portfolio),
    _solve(
        "resolve_with_overrides",
        (m.ResolveInput, PortfolioRecord),
        h.resolve_with_overrides,
    ),
    _solve("insert_rush_order", (m.InsertRushInput, RushRecord), h.insert_rush_order),
    _simulate("simulate_plan", (m.SimulateInput, SimulateResult), h.simulate_plan),
    _simulate(
        "simulate_portfolio",
        (m.SimulatePortfolioInput, PortfolioSimulation),
        h.simulate_portfolio,
    ),
    _keyed(
        "schedule_operations",
        (m.ScheduleOperationsInput, m.ScheduleOutput),
        h.schedule_operations,
        timeout=SCHEDULE_TIMEOUT_SECONDS,
    ),
    _keyed(
        "solve_schedule_snapshot",
        (m.SolveScheduleSnapshotInput, m.SolveSnapshotOutput),
        h.solve_schedule_snapshot,
        timeout=SCHEDULE_TIMEOUT_SECONDS,
    ),
    _keyed(
        "simulate_schedule_baseline",
        (m.ScheduleBaselineInput, m.JobOutput),
        h.simulate_schedule_baseline,
    ),
    _keyed("evaluate", (m.EvaluateInput, m.JobOutput), h.evaluate),
    _spec(
        "cancel_job",
        (m.JobIdInput, m.JobOutput),
        h.cancel_job,
        "state_modifying",
        destructive=True,
    ),
    # --- state_modifying, append-only -------------------------------------------------------
    _append("load_example", (m.ExampleInput, m.ScenarioOutput), h.load_example),
    _append("import_scenario", (m.ImportScenarioInput, m.ScenarioOutput), h.import_scenario),
    _append("branch_scenario", (m.BranchScenarioInput, m.ScenarioOutput), h.branch_scenario),
    _append("save_building_brief", (m.BriefInput, m.BriefOutput), h.save_building_brief),
    _append("answer_building_brief", (m.AnswerBriefInput, m.BriefOutput), h.answer_building_brief),
    _append("save_model_draft", (m.SaveModelDraftInput, m.ModelDraftOutput), h.save_model_draft),
    _append("import_model_draft", (m.DraftIdInput, m.ScenarioOutput), h.import_model_draft),
    _append(
        "commit_schedule_upload",
        (m.UploadCommitInput, m.ScheduleSnapshotOutput),
        h.commit_schedule_upload,
    ),
    _append(
        "ingest_operational_events",
        (m.IngestEventsInput, m.IngestOutput),
        h.ingest_operational_events,
    ),
    _append(
        "build_operational_snapshot",
        (m.BuildOperationalSnapshotInput, m.OperationalSnapshotOutput),
        h.build_operational_snapshot,
    ),
    _append("propose_action", (m.ProposeActionInput, m.ProposalOutput), h.propose_action),
    _admin(
        "put_planning_profile",
        (m.PutPlanningProfileInput, ArtifactRef),
        h.put_planning_profile,
    ),
    _admin("put_k_table", (m.PutKTableInput, ArtifactRef), h.put_k_table),
)


SPECS: Mapping[str, ToolSpec] = {spec.name: spec for spec in CATALOGUE}
