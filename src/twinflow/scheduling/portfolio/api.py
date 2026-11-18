"""Planning functions for TF-005 (COMP-029, REQ-126; TF-005 SDD "TF-003 (dependency)").

The TF-005 planning service is the only caller; the facade re-exports these four. Pure apart
from the clock (`time.monotonic`, inside the solvers and the runner), no I/O, Pydantic in and
out:

- `solve_portfolio`: the portfolio runner (COMP-024), as is.
- `get_pareto_menu(result, axes=None)`: the stored menu for None or the menu's own axes;
  for other axes the menu recomputed from the verified candidates' stored values (no solver
  runs). Axes must be 2 or 3 distinct kinds (`MenuAxes`), else ValueError.
- `resolve_with_overrides(request, previous_plan, overrides, ...)`: the request with the
  previous plan set and the overrides appended to its own, run as a portfolio with
  `reschedule` first (index 00) and then the profile portfolio. The runner adds churn to the
  effective objective because the request now has a previous plan.
- `insert_rush_order(request, previous_plan, rush_job, ...)`: the rush job appended to the
  problem, solved by `rush_insert` with the previous plan; relaxed due dates are in
  `result.schedule.relaxations`. Without the `rush_insert` leaf: UNKNOWN `unknown_solver`.

Inputs are never mutated; the changed request is validated again, so the model's size caps
hold (an over-cap request raises pydantic's ValidationError, a ValueError).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import TypeAdapter

from twinflow.scheduling import registry
from twinflow.scheduling.model import (
    Job,
    MenuAxes,
    MenuEntry,
    ObjectiveKind,
    Override,
    PortfolioResult,
    PreviousPlan,
    Problem,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.portfolio.pareto import menu
from twinflow.scheduling.portfolio.runner import solve_portfolio
from twinflow.scheduling.solver import solve

RESCHEDULE = "reschedule"
RUSH_INSERT = "rush_insert"
_MENU_AXES = TypeAdapter(MenuAxes)

__all__ = ["get_pareto_menu", "insert_rush_order", "resolve_with_overrides", "solve_portfolio"]


def get_pareto_menu(
    result: PortfolioResult, axes: Sequence[ObjectiveKind] | None = None
) -> tuple[MenuEntry, ...]:
    """The Pareto menu of `result` over `axes` (module docstring); ValueError for bad axes."""
    if axes is None:
        return result.menu
    checked = _MENU_AXES.validate_python(tuple(axes))
    if result.menu and checked == tuple(result.menu[0].values):
        return result.menu
    verified = {c.id: c.objective_values for c in result.candidates if c.verified}
    return menu(verified, checked)


def resolve_with_overrides(
    request: SolveRequest,
    previous_plan: PreviousPlan,
    overrides: Sequence[Override],
    *,
    budget_seconds: float | None = None,
    seed: int = 0,
) -> PortfolioResult:
    """Re-plan from `previous_plan` under the request's and the new overrides (COMP-029)."""
    changed = SolveRequest.model_validate(
        dict(request)
        | {"previous_plan": previous_plan, "overrides": (*request.overrides, *overrides)}
    )
    pipelines = (RESCHEDULE, *request.profile.default_portfolio)
    return solve_portfolio(changed, budget_seconds=budget_seconds, seed=seed, pipelines=pipelines)


def insert_rush_order(
    request: SolveRequest,
    previous_plan: PreviousPlan,
    rush_job: Job,
    *,
    budget_seconds: float | None = None,
    seed: int = 0,
) -> SolveResult:
    """Insert `rush_job` into `previous_plan` with `rush_insert` (COMP-020, COMP-029)."""
    problem = Problem.model_validate(
        dict(request.problem) | {"jobs": (*request.problem.jobs, rush_job)}
    )
    changed = SolveRequest.model_validate(
        dict(request) | {"problem": problem, "previous_plan": previous_plan}
    )
    try:
        solver = registry.get(RUSH_INSERT)
    except registry.UnknownSolverError:
        return _unavailable(RUSH_INSERT)
    return solve(changed, solver=solver, budget_seconds=budget_seconds, seed=seed)


def _unavailable(name: str) -> SolveResult:
    """UNKNOWN with `unknown_solver`: the leaf providing `name` is not installed."""
    message = f"no solver is registered as {name!r}"
    issue = ScheduleIssue(code="unknown_solver", path="solver", message=message)
    schedule = Schedule(status="UNKNOWN", assignments=(), solver=name, elapsed_seconds=0.0)
    return SolveResult(schedule=schedule, verified=False, issues=(issue,))
