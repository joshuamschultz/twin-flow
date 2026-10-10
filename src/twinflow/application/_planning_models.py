"""Planning service (COMP-117) errors and typed results.

Every result is provisional evidence (REQ-177): it carries `validity = "provisional"` and an
`evidence_kind` naming what produced it. Plans and portfolios are returned by reference
(D-119); `get_plan` pages the assignments.
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


class CompareResult(_Result):
    """Plans of one problem side by side, ranked best first by the profile objective."""

    problem_id: str
    plans: tuple[CompareRow, ...]
    ranking: tuple[str, ...]
    validity: Validity = "provisional"
    evidence_kind: Literal["plan_comparison"] = "plan_comparison"


class SimulateResult(_Result):
    """A simulation request outcome: the submitted job, or why none could be submitted."""

    plan_id: str
    status: str
    issues: tuple[ScheduleIssue, ...]
    job: dict[str, Any] | None
    validity: Validity = "provisional"
    evidence_kind: Literal["simulation"] = "simulation"
