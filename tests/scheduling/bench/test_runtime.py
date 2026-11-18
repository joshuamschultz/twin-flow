"""The SDD runtime budget table on the 600 x 8 scale instance (T-167; T-133, T-139, T-143,
T-144, T-149..T-152, T-155, T-160; COMP-027, REQ-125).

SDD "Runtime budgets": each method on `scale_instance()` (600 jobs x 8 ops, 8 centers of 5
machines, HMLV data; five_phase on its flow-line 600 x 8) returns a verified schedule within
its budget, or, where the table says so, a named refusal fast. Time is wall clock around the
call (validation, compile, the method, verification); building the instance, the previous
plan and the Pareto seeds is outside the timing. Every test prints its elapsed time.

Rows, one per SDD row:

- `VERIFIED_BUDGETS`: dispatch rules, atc, atcs, forward_load, backward_load 10 s; gt_active
  15 s; five_phase 20 s; sb_makespan, sb_wt 90 s (defaults: reopt_rounds 2 plus the final
  round, DEV-16, stricter than the SDD's reopt_rounds 1); rolling_horizon 60 s. Construction
  methods run with the `atcs` rule they are gated with (DEV-23).
- `REFUSAL_BUDGETS`: ortools on the whole instance, `model_too_large` in under 2 s. Its "40 x 5
  window: budget" half: a verified plan on one 40 x 5 HMLV instance within budget + 10 percent.
- `ANYTIME_BUDGETS`: tabu, sa, beam at a 30 s budget, held to budget + 10 percent; Pareto
  search likewise (`pareto_search` from the atcs and edd plans, deadline 30 s).
- rush_insert (one 8-op rush job, `insert_rush_order`) 10 s; reschedule (one new 8-op job
  against the atcs plan) budget + 10 percent at 30 s; `solve_portfolio` with the bench
  portfolio budget + 10 percent + 1 s at 120 s (about 2 minutes on its own).
"""

from __future__ import annotations

import functools
import importlib.util
import time
from collections.abc import Callable
from typing import Any

import pytest

from bench.gates import BENCH_PORTFOLIO
from bench.hmlv import TRAINING_SEEDS, hmlv_instance, scale_flow_line_instance, scale_instance
from twinflow.scheduling import available_solvers, insert_rush_order, solve_portfolio
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import (
    Job,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.portfolio.pareto_search import Seed, pareto_search
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.solver import check_schedule, prepare, solve

ANYTIME_BUDGET = 30.0
ANYTIME_LIMIT = ANYTIME_BUDGET * 1.1
RULE_ATCS = {"rule": "atcs"}

Instance = Callable[[], SolveRequest]


@functools.cache
def _scale() -> SolveRequest:
    return scale_instance(0)


@functools.cache
def _flow_scale() -> SolveRequest:
    return scale_flow_line_instance(0)


def _dispatch_rules() -> tuple[str, ...]:
    return tuple(info.name for info in available_solvers() if info.family == "dispatch")


VERIFIED_BUDGETS: tuple[tuple[str, float, dict[str, Any], Instance], ...] = (
    *((rule, 10.0, {}, _scale) for rule in _dispatch_rules()),
    ("atc", 10.0, {}, _scale),
    ("atcs", 10.0, {}, _scale),
    ("forward_load", 10.0, RULE_ATCS, _scale),
    ("backward_load", 10.0, RULE_ATCS, _scale),
    ("gt_active", 15.0, RULE_ATCS, _scale),
    ("five_phase", 20.0, {}, _flow_scale),
    ("sb_makespan", 90.0, {}, _scale),
    ("sb_wt", 90.0, {}, _scale),
    ("rolling_horizon", 60.0, {}, _scale),
)
"""(method, seconds, method params, instance): a verified schedule in under `seconds`, with
`seconds` also the budget handed to the solver."""

REFUSAL_BUDGETS: tuple[tuple[str, str, float, float], ...] = (
    ("ortools", "model_too_large", 2.0, 10.0),
)
"""(method, issue code, seconds, budget handed to the solver): UNKNOWN with that code fast."""

ANYTIME_METHODS = ("tabu", "sa", "beam")
"""Each at `ANYTIME_BUDGET`, held to `ANYTIME_LIMIT` with a verified schedule."""

CPSAT_WINDOW_BUDGET = 5.0
RUSH_SECONDS = 10.0
RESCHEDULE_BUDGET = 30.0
PORTFOLIO_BUDGET = 120.0
PORTFOLIO_LIMIT = PORTFOLIO_BUDGET * 1.1 + 1.0

needs_ortools = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None, reason="ortools (scheduling extra) is absent"
)


def _with_params(request: SolveRequest, method: str, params: dict[str, Any]) -> SolveRequest:
    if not params:
        return request
    profile = request.profile.model_copy(update={"method_params": {method: params}})
    return request.model_copy(update={"profile": profile})


def _report(label: str, elapsed: float, limit: float, status: str) -> None:
    print(f"\n{label:<16} {elapsed:8.2f} s  (limit {limit:g} s)  {status}")


def _timed_solve(
    method: str, request: SolveRequest, budget: float, limit: float
) -> tuple[SolveResult, float]:
    solver = get_solver(method)
    start = time.perf_counter()
    result = solve(request, solver=solver, budget_seconds=budget)
    elapsed = time.perf_counter() - start
    _report(method, elapsed, limit, result.schedule.status)
    return result, elapsed


def _codes(result: SolveResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _assert_complete(method: str, result: SolveResult, request: SolveRequest) -> None:
    assert result.verified, (method, result.schedule.status, _codes(result))
    ops = sum(len(job.operations) for job in request.problem.jobs)
    assert len(result.schedule.assignments) == ops, method


# --- verified within the budget ---------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize(
    ("method", "seconds", "params", "instance"),
    VERIFIED_BUDGETS,
    ids=[row[0] for row in VERIFIED_BUDGETS],
)
def test_method_returns_a_verified_schedule_on_600x8_within_its_budget(
    method: str, seconds: float, params: dict[str, Any], instance: Instance
) -> None:
    request = _with_params(instance(), method, params)
    result, elapsed = _timed_solve(method, request, seconds, seconds)

    _assert_complete(method, result, request)
    assert len(result.schedule.assignments) == 600 * 8
    assert elapsed < seconds, f"{method} took {elapsed:.2f} s, budget {seconds} s"


def test_every_dispatch_rule_has_a_runtime_row() -> None:
    rows = {row[0] for row in VERIFIED_BUDGETS}
    assert set(_dispatch_rules()) <= rows
    assert len(_dispatch_rules()) == 10, _dispatch_rules()


# --- anytime: budget + 10 percent -------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.parametrize("method", ANYTIME_METHODS)
def test_anytime_method_returns_a_verified_schedule_on_600x8_within_budget_plus_10_percent(
    method: str,
) -> None:
    result, elapsed = _timed_solve(method, _scale(), ANYTIME_BUDGET, ANYTIME_LIMIT)

    _assert_complete(method, result, _scale())
    assert elapsed < ANYTIME_LIMIT, f"{method} took {elapsed:.2f} s, limit {ANYTIME_LIMIT}"


def _seed_of(schedule: Schedule) -> Seed:
    """The plan's machine choice and start-ordered machine sequences."""
    ordered = sorted(schedule.assignments, key=lambda a: (a.start, a.operation_id))
    sequences: dict[str, list[str]] = {}
    for a in ordered:
        sequences.setdefault(a.machine_id, []).append(a.operation_id)
    return {a.operation_id: a.machine_id for a in ordered}, sequences


@pytest.mark.slow
def test_pareto_search_on_600x8_returns_within_budget_plus_10_percent_with_verified_points() -> (
    None
):
    request = _scale()
    seeds = []
    for rule in ("atcs", "edd"):
        result = solve(request, solver=get_solver(rule), budget_seconds=10.0)
        assert result.verified, (rule, _codes(result))
        seeds.append(_seed_of(result.schedule))
    prepared = prepare(request, ANYTIME_BUDGET)
    assert prepared.compiled is not None, prepared.issues
    axes = request.profile.menu_axes

    start = time.perf_counter()
    index = ProblemIndex.build(prepared.compiled.problem)
    points = pareto_search(index, seeds, axes, time.monotonic() + ANYTIME_BUDGET)
    elapsed = time.perf_counter() - start
    _report("pareto_search", elapsed, ANYTIME_LIMIT, f"{len(points)} points")

    assert points, "the archive keeps at least the seeds' non-dominated points"
    assert elapsed < ANYTIME_LIMIT, f"pareto search took {elapsed:.2f} s"
    for point in (points[0], points[-1]):
        decoded = decode(index, point.choice, point.sequences)
        assert isinstance(decoded, Decoded), decoded
        checked = check_schedule(prepared, decoded.schedule("pareto_search"))
        assert checked.verified, _codes(checked)


# --- named refusal fast; CP-SAT on a 40 x 5 window -------------------------------------------


@pytest.mark.slow
@needs_ortools
@pytest.mark.parametrize(
    ("method", "code", "seconds", "budget"), REFUSAL_BUDGETS, ids=[r[0] for r in REFUSAL_BUDGETS]
)
def test_method_refuses_600x8_by_name_fast(
    method: str, code: str, seconds: float, budget: float
) -> None:
    result, elapsed = _timed_solve(method, _scale(), budget, seconds)

    assert result.schedule.status == "UNKNOWN", result.schedule.status
    assert code in _codes(result), _codes(result)
    assert elapsed < seconds, f"{method} took {elapsed:.2f} s to refuse, limit {seconds} s"


@pytest.mark.slow
@needs_ortools
def test_ortools_on_a_40x5_window_returns_a_verified_plan_within_budget_plus_10_percent() -> None:
    request = hmlv_instance(TRAINING_SEEDS[0])
    limit = CPSAT_WINDOW_BUDGET * 1.1
    result, elapsed = _timed_solve("ortools", request, CPSAT_WINDOW_BUDGET, limit)

    _assert_complete("ortools", result, request)
    assert elapsed < limit, f"ortools took {elapsed:.2f} s on 40 x 5, limit {limit:.2f} s"


# --- reactive: rush insertion and reschedule ---------------------------------------------------


def _plan_of(schedule: Schedule, plan_id: str) -> PreviousPlan:
    return PreviousPlan(
        plan_id=plan_id,
        problem_digest=f"{plan_id}-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
            for a in schedule.assignments
        ),
    )


@functools.cache
def _atcs_plan() -> PreviousPlan:
    result = solve(_scale(), solver=get_solver("atcs"), budget_seconds=10.0)
    assert result.verified, _codes(result)
    return _plan_of(result.schedule, "scale-atcs")


def _copy_job(template: Job, job_id: str, **fields: Any) -> Job:
    """`template`'s 8 ops renamed `{job_id}-k` and chained, with `fields` replaced."""
    data = template.model_dump(mode="json")
    operations = []
    for k, operation in enumerate(data["operations"]):
        copy = dict(operation, id=f"{job_id}-{k}")
        copy["predecessors"] = [{"operation_id": f"{job_id}-{k - 1}"}] if k else []
        operations.append(copy)
    return Job.model_validate(data | {"id": job_id, "operations": operations} | fields)


@pytest.mark.slow
def test_rush_insert_of_one_8_op_job_on_600x8_is_verified_under_10_seconds() -> None:
    request, plan = _scale(), _atcs_plan()
    horizon = max(a.start for a in plan.assignments)
    rush = _copy_job(
        request.problem.jobs[0], "RUSH", weight_class=4, weight_override=None, due=10 * horizon
    )

    start = time.perf_counter()
    result = insert_rush_order(request, plan, rush, budget_seconds=RUSH_SECONDS)
    elapsed = time.perf_counter() - start
    _report("rush_insert", elapsed, RUSH_SECONDS, result.schedule.status)

    assert result.verified, (result.schedule.status, _codes(result))
    placed = {a.operation_id for a in result.schedule.assignments}
    assert {f"RUSH-{k}" for k in range(8)} <= placed
    assert len(placed) == 600 * 8 + 8
    assert elapsed < RUSH_SECONDS, f"rush_insert took {elapsed:.2f} s"


@pytest.mark.slow
def test_reschedule_on_600x8_returns_a_verified_plan_within_budget_plus_10_percent() -> None:
    base, plan = _scale(), _atcs_plan()
    template = base.problem.jobs[1]
    new_job = _copy_job(template, "NEW", due=template.due)
    problem = base.problem.model_copy(update={"jobs": (*base.problem.jobs, new_job)})
    request = SolveRequest.model_validate(dict(base) | {"problem": problem, "previous_plan": plan})
    limit = RESCHEDULE_BUDGET * 1.1

    result, elapsed = _timed_solve("reschedule", request, RESCHEDULE_BUDGET, limit)

    _assert_complete("reschedule", result, request)
    assert elapsed < limit, f"reschedule took {elapsed:.2f} s, limit {limit:.2f} s"


# --- the portfolio ----------------------------------------------------------------------------


@pytest.mark.slow
def test_solve_portfolio_on_600x8_within_budget_plus_10_percent_plus_1_second_at_120_s() -> None:
    request = _scale()

    start = time.perf_counter()
    result = solve_portfolio(
        request, budget_seconds=PORTFOLIO_BUDGET, pipelines=BENCH_PORTFOLIO.pipelines
    )
    elapsed = time.perf_counter() - start
    _report("solve_portfolio", elapsed, PORTFOLIO_LIMIT, str(result.recommended))

    assert result.recommended is not None, [i.code for i in result.issues]
    recommended = next(c for c in result.candidates if c.id == result.recommended)
    assert recommended.verified, recommended.issues
    assert len(recommended.schedule.assignments) == 600 * 8
    assert elapsed < PORTFOLIO_LIMIT, f"solve_portfolio took {elapsed:.2f} s"
