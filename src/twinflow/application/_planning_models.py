"""Planning service (COMP-117) errors and typed results.

Every result is provisional evidence (REQ-177): it carries `validity = "provisional"` and an
`evidence_kind` naming what produced it. Plans and portfolios are returned by reference
(D-119); `get_plan` pages the assignments. Simulation outputs follow the TF-004 SDD Data
Model "Outputs" (`CandidateSummary`, `SimulateResult`, `PortfolioSimulation`, the
`CompareResult.simulation` block) and are built from `twinflow.execution` results by
attribute (`from_attributes`), so this module never imports the execution engine.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from twinflow.erp.issues import ErpIssue
from twinflow.erp.schema import Lineage
from twinflow.scheduling import ScheduleIssue

Validity = Literal["provisional"]


class PlanningError(Exception):
    """A planning call failed in a way the caller can act on; `code` is stable."""

    code: str = "planning_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFound(PlanningError):
    """A referenced problem, plan or portfolio id is not stored."""

    code = "not_found"


class IdempotencyConflict(PlanningError):
    """The caller's request key was already used with different inputs (D-120)."""

    code = "idempotency_conflict"


class PlanningTimeout(PlanningError):
    """The solver ran past its budget plus the grace period (REQ-169)."""

    code = "timeout"


class Blocked(PlanningError):
    """The inputs have hard issues; `issues` names each one."""

    code = "blocked"

    def __init__(self, message: str, issues: tuple[ErpIssue, ...]) -> None:
        super().__init__(message)
        self.issues = issues


class SizeLimit(PlanningError):
    """An input or result is larger than the service accepts."""

    code = "size_limit"


class CapabilityUnavailable(PlanningError):
    """The capability is not installed (TF-003 absent) or not built yet (B-6)."""

    code = "capability_unavailable"


class _Result(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BuildRecord(_Result):
    """The outcome of `build_problem`: ids of the stored records plus the build report."""

    status: Literal["ready", "blocked"]
    problem_id: str | None
    problem_digest: str | None
    snapshot_digest: str
    profile_digest: str
    builder_version: str
    counts: dict[str, int]
    issues: tuple[ErpIssue, ...]
    issue_total: int
    validity: Validity = "provisional"
    evidence_kind: Literal["erp_build"] = "erp_build"


class PlanStatus(_Result):
    """One candidate plan of a portfolio: its id, solver status and verification."""

    plan_id: str
    status: str
    verified: bool


class PortfolioRecord(_Result):
    """A stored portfolio by reference: plan ids, the recommended plan, the menu size."""

    portfolio_id: str
    problem_id: str
    plan_ids: tuple[str, ...]
    recommended_plan_id: str | None
    menu_size: int
    statuses: tuple[PlanStatus, ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["portfolio"] = "portfolio"


class RushRecord(_Result):
    """A stored rush-insert plan by reference, with the due dates TF-003 had to relax."""

    plan_id: str
    problem_id: str
    status: str
    verified: bool
    relaxations: tuple[dict[str, Any], ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["rush_insert"] = "rush_insert"


class PlanAssignment(_Result):
    """One scheduled operation with the ERP row it came from (None for a rush operation)."""

    operation_id: str
    job_id: str | None
    machine_id: str
    start: float
    setup_end: float
    end: float
    batch_id: str | None
    lineage: Lineage | None


class PlanPage(_Result):
    """One page of a plan's assignments, in stored order (REQ-168)."""

    plan_id: str
    problem_id: str
    total: int
    offset: int
    limit: int
    next_offset: int | None
    assignments: tuple[PlanAssignment, ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["plan"] = "plan"


class MenuRow(_Result):
    """One Pareto menu entry, pointing at its plan."""

    plan_id: str
    candidate_id: str
    supported: bool
    values: dict[str, float]


class MenuRecord(_Result):
    """The stored Pareto menu of one portfolio."""

    portfolio_id: str
    entries: tuple[MenuRow, ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["pareto_menu"] = "pareto_menu"


class CompareRow(_Result):
    """One plan's objective values in a comparison."""

    plan_id: str
    objective_values: dict[str, float]
    verified: bool


class _FromAttributes(_Result):
    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)


class Stats(_FromAttributes):
    """One metric over replications; `ci_*` None when n < 2."""

    mean: float
    std: float
    min: float
    p10: float
    p50: float
    p90: float
    max: float
    ci_low: float | None
    ci_high: float | None
    n: int


class PairedDelta(_FromAttributes):
    """Candidate minus reference over common replications (D-103); bounds None when n < 2."""

    mean_difference: float
    low: float | None
    high: float | None
    n: int


class Bottleneck(_FromAttributes):
    """The planned top machine by busy share."""

    machine_id: str
    planned_utilization: float


class DeltaPoint(_FromAttributes):
    """One robustness probe: an outage of `delta_seconds` on `machine_id` at now."""

    delta_seconds: float
    machine_id: str
    objective: float
    increase: float
    slope: float


class LoadViolation(_FromAttributes):
    """A machine whose planned load in one week exceeds its target."""

    machine_id: str
    week: int
    load: float
    target: float


class LoadCheck(_FromAttributes):
    """The weekly load check (at most 100 violations listed)."""

    weeks: int
    violations_total: int
    worst_excess: float
    violations: tuple[LoadViolation, ...]


class Robustness(_FromAttributes):
    """Planned robustness, the outage delta curve, expected objective and load check."""

    planned_r: float
    delta_curve: tuple[DeltaPoint, ...]
    expected_objective: float | None
    load_check: LoadCheck


class CandidateSummary(_Result):
    """One simulated plan or dispatch baseline (`plan_id` None, no planned analyses)."""

    plan_id: str | None
    simulation_id: str
    kind: Literal["plan", "baseline"]
    replications: int
    deterministic: bool
    status: Literal["completed", "truncated"]
    seed: int
    planned: dict[str, float]
    metrics: dict[str, Stats]
    bottleneck: Bottleneck | None
    robustness: Robustness | None
    baseline_delta: dict[str, PairedDelta] | None = None


class SimulateResult(_Result):
    """`simulate_plan`: the stored simulation by reference, or why the plan was blocked."""

    plan_id: str
    status: Literal["completed", "truncated", "blocked"]
    simulation_id: str | None
    issues: tuple[ScheduleIssue, ...]
    summary: CandidateSummary | None
    baseline: CandidateSummary | None
    validity: Validity = "provisional"
    evidence_kind: Literal["simulation"] = "simulation"


class PortfolioSimulation(_Result):
    """`simulate_portfolio`: candidates in simulation order (D-108), baseline and ranking."""

    evaluation_id: str | None
    portfolio_id: str
    status: Literal["completed", "truncated", "blocked"]
    replications: int
    seed: int
    candidates: tuple[CandidateSummary, ...]
    baseline: CandidateSummary | None
    ranking: tuple[str, ...]
    issues: tuple[ScheduleIssue, ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["portfolio_simulation"] = "portfolio_simulation"


class SimulationComparison(_Result):
    """Stored simulations of compared plans: paired deltas against the first plan."""

    seed: int
    replications: int
    reference_plan_id: str
    deltas: dict[str, dict[str, PairedDelta]]
    ranking: tuple[str, ...]


class CompareResult(_Result):
    """Plans of one problem side by side, ranked best first by the profile objective."""

    problem_id: str
    plans: tuple[CompareRow, ...]
    ranking: tuple[str, ...]
    simulation: SimulationComparison | None = None
    validity: Validity = "provisional"
    evidence_kind: Literal["plan_comparison"] = "plan_comparison"


class SimulationEvidencePage(_Result):
    """One page of a simulation's evidence rows: operation rows, then job rows (REQ-141)."""

    simulation_id: str
    total: int
    offset: int
    limit: int
    next_offset: int | None
    rows: tuple[dict[str, Any], ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["simulation"] = "simulation"
