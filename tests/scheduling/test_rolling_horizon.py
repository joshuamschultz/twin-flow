"""Rolling horizon: cuts, windows, seams and the scale gate (T-151, T-152; COMP-015, COMP-027,
REQ-103, REQ-125; SDD "Rolling horizon", Pinedo Sec 15.2 p.398-403).

API these tests pin (the implementer builds to it):

- `twinflow.scheduling.algorithms.rolling_horizon` registers `rolling_horizon` (family
  `decomposition`, `deterministic=True`: its default window solver is deterministic).
  Params through `profile.method_params["rolling_horizon"]`: `gap`, `eps`, `min_jobs`
  (default 20), `max_jobs` (150), `seam` (5; 0 = seams off), `window_solver` (default
  `"atcs->tabu"`: registry names joined by `->`, run in series, each stage's verified
  schedule is the next stage's `SolveContext.warm_start`) and `seam_solver` (default
  `ortools` when registered and the seam is small, else `tabu`). Invalid values are refused
  as UNKNOWN `invalid_method_param`; a window or seam solver name the registry does not
  know gives UNKNOWN `subsolver_unavailable` (D-069 leaf rule).
- `cuts(index, params) -> tuple[Cut, ...]`, a pure function on a `ProblemIndex`: the cuts
  after `min_jobs` / `max_jobs` merging, in ascending order. `Cut(after, reason)`: `after`
  is the 0-based position, in the job list sorted by (due, id), of the last job before the
  cut; `reason` is `"due_gap"` (d_next - d_a >= gap * pbar_job, pbar_job = mean total
  shortest run per job) or `"load"` (V(t) <= eps * capacity, V(t) = released work on the
  bottleneck not yet processed just before t, t = the first release after job a). The
  windows are the runs of jobs between cuts.
- Window w's sub-problem holds window w's jobs plus every earlier window's ops as exact
  pins (`pin.machine_id`, `pin.earliest_start == pin.latest_start` = the start); later
  windows' jobs are absent. The sub-solver gets it through `Solver.solve` (registry name
  looked up at call time).

Fixture pinedo_15_2_3 (1 || sum w_j T_j, p and d of Ex 15.2.2): p = 7, 6, 7, 6, 5, 5, 8,
9, 4; d = 22, 24, 24, 25, 33, 51, 52, 52, 55; w = 0, 1, 1, 2, 1, 1, 2, 1, 4 (DEV-8: job 1's
weight 0 is encoded as 1e-6). With gap 1 the due gaps 25 -> 33 (8) and 33 -> 51 (18) reach
pbar_job = 57 / 9; min_jobs 2 merges the lone job 5 into the next window, so the cut falls
between jobs 4 and 5 (p.401). Windows {1..4} and {5..9} solved exactly: window 1 puts job 1
last (T1 = 4, 4e-6), window 2 starts at 26 and its best is job 8 last (T8 = 5): the book's
naive value 5 (p.402); the true optimum is 0 under the book's weights (35e-6 here).
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest
from bench.hmlv import TRAINING_SEEDS, hmlv_instance
from kernel_cases import compile_request, load_request, machine, op, request_from
from oracle import brute_force
from oracle_math import objective_values

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    ObjectiveKind,
    ObjectiveSpec,
    Problem,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.registry import SolverSpec, available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, SolveContext
from twinflow.scheduling.solver import solve

WT = ObjectiveKind.WEIGHTED_TARDINESS
NAME = "rolling_horizon"
ORACLE = "oracle_ws"
TINY_WEIGHT = 1e-6  # DEV-8: job 1's book weight 0
NAIVE_BOOK_VALUE = 5.0  # p.402: the decomposition at the 4|5 cut, solved window by window
WINDOW_1 = ("1-1", "2-1", "3-1", "4-1")
WINDOW_2 = ("5-1", "6-1", "7-1", "8-1", "9-1")

needs_ortools = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None, reason="ortools (scheduling extra) is absent"
)


# --- helpers ---------------------------------------------------------------------------------


def _rh() -> ModuleType:
    """The leaf, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.algorithms.rolling_horizon")


def _with_params(request: SolveRequest, params: Mapping[str, Any]) -> SolveRequest:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {NAME: dict(params)}
    return SolveRequest.model_validate(data)


def _solve(request: SolveRequest, params: Mapping[str, Any], **kwargs: Any) -> SolveResult:
    return solve(_with_params(request, params), solver=get_solver(NAME), **kwargs)


def _codes(result: SolveResult) -> list[str]:
    return [i.code for i in (*result.schedule.issues, *result.issues)]


def _index(request: SolveRequest) -> ProblemIndex:
    return ProblemIndex.build(compile_request(request).problem)


def _cuts(request: SolveRequest, params: Mapping[str, Any]) -> list[tuple[int, str]]:
    return [(c.after, c.reason) for c in _rh().cuts(_index(request), params)]


def _window_sizes(request: SolveRequest, params: Mapping[str, Any]) -> list[int]:
    jobs = len(request.problem.jobs)
    bounds = [-1, *(c.after for c in _rh().cuts(_index(request), params)), jobs - 1]
    return [b - a for a, b in zip(bounds, bounds[1:], strict=False)]


def _book_value(result: SolveResult) -> float:
    """DEV-8: weighted tardiness under the book's weights (job 1 weight 0) from the plan."""
    request = load_request("pinedo_15_2_3")
    jobs = {j.id: j for j in request.problem.jobs}
    total = 0.0
    for a in result.schedule.assignments:
        job = jobs[a.operation_id.split("-")[0]]
        if job.id != "1" and job.due is not None:
            total += (job.weight_override or 1.0) * max(0.0, a.end - job.due)
    return total


def _tardiness_of_job_1(result: SolveResult) -> float:
    end = {a.operation_id: a.end for a in result.schedule.assignments}["1-1"]
    return max(0.0, end - 22.0)


def _exact_pin(problem: Problem, op_id: str) -> tuple[str | None, float | None] | None:
    """(machine, start) when `op_id` carries an exact pin, else None."""
    for job in problem.jobs:
        for operation in job.operations:
            if operation.id != op_id or operation.pin is None:
                continue
            pin = operation.pin
            if pin.earliest_start is None or pin.earliest_start != pin.latest_start:
                return None
            return pin.machine_id, pin.earliest_start
    return None


def _op_ids(problem: Problem) -> set[str]:
    return {o.id for j in problem.jobs for o in j.operations}


# --- a brute-force window solver injected into the registry ----------------------------------


@dataclass
class _Call:
    problem: Problem
    warm_start: Schedule | None
    returned: Schedule


@dataclass
class _OracleWindowSolver:
    """A single-machine exact window solver: exactly pinned ops stay where they are pinned,
    the rest are brute-forced (tests/scheduling/oracle.py) after the pinned block.

    The oracle refuses pins, so the wrapper drops pinned jobs, starts the machine at the
    pinned block's end and adds the pinned assignments back. Anything else it cannot model
    answers UNKNOWN `oracle_unsupported` (never a silent wrong answer).
    """

    name: str
    calls: list[_Call] = field(default_factory=list)

    @property
    def capabilities(self) -> frozenset[Feature]:
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        schedule = self._solve(problem)
        self.calls.append(_Call(problem, context.warm_start, schedule))
        return schedule

    def _solve(self, problem: Problem) -> Schedule:
        if len(problem.machines) != 1:
            return self._unknown("one machine only")
        (machine_id,) = (m.id for m in problem.machines)
        pinned: list[Assignment] = []
        free: list[Any] = []
        for job in problem.jobs:
            ops = job.operations
            pins = [_exact_pin(problem, o.id) for o in ops]
            if all(p is None for p in pins) and all(o.pin is None for o in ops):
                free.append(job)
                continue
            if any(p is None or p[0] not in (None, machine_id) for p in pins):
                return self._unknown(f"job {job.id}: not an exact pin on {machine_id}")
            for operation, pin in zip(ops, pins, strict=True):
                assert pin is not None and pin[1] is not None
                run = operation.options[0].fixed_seconds
                run += operation.quantity * operation.options[0].unit_seconds
                pinned.append(
                    Assignment(
                        operation_id=operation.id,
                        machine_id=machine_id,
                        start=pin[1],
                        setup_end=pin[1],
                        end=pin[1] + run,
                    )
                )
        block_end = max((a.end for a in pinned), default=problem.now)
        assignments = list(pinned)
        if free:
            reduced = problem.model_copy(
                update={
                    "jobs": tuple(free),
                    "machines": tuple(
                        m.model_copy(
                            update={
                                "state": m.state.model_copy(
                                    update={
                                        "available_from": max(m.state.available_from, block_end)
                                    }
                                )
                            }
                        )
                        for m in problem.machines
                    ),
                }
            )
            _, best = brute_force(reduced, WT)
            assignments += best.assignments
        plan = Schedule(
            status="FEASIBLE", assignments=tuple(assignments), solver=self.name, elapsed_seconds=0.0
        )
        value = objective_values(
            CompiledProblem(problem=problem, profile=SchedulingProfile()), plan
        )
        return plan.model_copy(update={"primary_value": value[WT]})

    def _unknown(self, why: str) -> Schedule:
        issue = ScheduleIssue(code="oracle_unsupported", path="problem", message=why)
        return Schedule(
            status="UNKNOWN", assignments=(), solver=self.name, elapsed_seconds=0.0, issues=(issue,)
        )


def _inject(monkeypatch: pytest.MonkeyPatch, *names: str) -> dict[str, _OracleWindowSolver]:
    """Register oracle window solvers under `names` (one shared instance per name).

    The registry's `_entries` is the single table behind `get`, `is_registered` and
    `available`, so patching it reaches the solver however the leaf imports `get`.
    """
    solvers = {name: _OracleWindowSolver(name) for name in names}
    entries = dict(registry._entries())
    for name, solver in solvers.items():
        spec = SolverSpec(
            name=name,
            family="test",
            factory=_constant(solver),
            deterministic=True,
            capabilities=frozenset(Feature),
        )
        entries[name] = (spec, __name__)
    monkeypatch.setattr(registry, "_entries", lambda: entries)
    return solvers


def _constant(solver: _OracleWindowSolver) -> Callable[..., _OracleWindowSolver]:
    def factory(**_params: object) -> _OracleWindowSolver:
        return solver

    return factory


# --- crafted instances -----------------------------------------------------------------------


def _release_gap_request() -> SolveRequest:
    """One machine; jobs 1, 2 released at 0 and 5, jobs 3, 4 at 100 and 105 (10 s each).

    Dues 120..123 rise by 1, so no due-gap cut at gap 100. Due order = release order.
    V just before 5 is 5 (job 1 half done), before 100 is 0 (jobs 1, 2 done at 20), before
    105 is 5: only the cut after job 2 (position 1) has V near zero.
    """
    jobs = [
        {"id": str(k), "release": r, "due": d, "operations": [op(f"{k}-1", {"M1": 10.0})]}
        for k, r, d in ((1, 0.0, 120.0), (2, 5.0, 121.0), (3, 100.0, 122.0), (4, 105.0, 123.0))
    ]
    problem = {
        "id": "release-gap",
        "jobs": jobs,
        "machines": [machine("M1")],
        "work_centers": [{"id": "WC"}],
    }
    return request_from(
        problem,
        profile={
            "objective": {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
        },
    )


def _flat_request(n: int) -> SolveRequest:
    """`n` one-op jobs on one machine, all released at 0 with the same due: no natural cut."""
    jobs = [
        {"id": f"J{k:02d}", "due": 50.0, "operations": [op(f"J{k:02d}-1", {"M1": 5.0})]}
        for k in range(n)
    ]
    problem = {
        "id": "flat",
        "jobs": jobs,
        "machines": [machine("M1")],
        "work_centers": [{"id": "WC"}],
    }
    return request_from(problem)


# --- registration and params -----------------------------------------------------------------


def test_rolling_horizon_is_registered_decomposition_deterministic() -> None:
    info = {i.name: i for i in available()}
    assert NAME in info, sorted(info)
    entry = info[NAME]
    assert entry.family == "decomposition"
    assert entry.deterministic is True
    assert entry.module == "twinflow.scheduling.algorithms.rolling_horizon"
    assert get_solver(NAME).name == NAME


@pytest.mark.parametrize(
    "params",
    [
        {"min_jobs": 0},
        {"max_jobs": 0},
        {"min_jobs": 10, "max_jobs": 5},
        {"seam": -1},
        {"gap": -1.0},
        {"eps": -0.1},
        {"window_solver": ""},
        {"no_such_param": 1},
    ],
    ids=[
        "min-0",
        "max-0",
        "min-above-max",
        "seam-negative",
        "gap-negative",
        "eps-negative",
        "window-solver-empty",
        "unknown-param",
    ],
)
def test_rolling_horizon_invalid_params_are_refused_by_name(params: dict[str, Any]) -> None:
    result = _solve(load_request("pinedo_15_2_3"), params)
    assert result.schedule.status == "UNKNOWN"
    assert "invalid_method_param" in _codes(result), _codes(result)


@pytest.mark.parametrize(
    "window_solver", ["no_such_solver", "atcs->no_such_solver", "no_such_solver->tabu"]
)
def test_missing_window_solver_returns_unknown_subsolver_unavailable(window_solver: str) -> None:
    result = _solve(load_request("pinedo_15_2_3"), {"min_jobs": 2, "window_solver": window_solver})
    assert result.schedule.status == "UNKNOWN"
    assert result.schedule.assignments == ()
    assert "subsolver_unavailable" in _codes(result), _codes(result)


def test_missing_seam_solver_returns_unknown_subsolver_unavailable() -> None:
    result = _solve(load_request("pinedo_15_2_3"), {"min_jobs": 2, "seam_solver": "no_such_solver"})
    assert result.schedule.status == "UNKNOWN"
    assert "subsolver_unavailable" in _codes(result), _codes(result)


# --- cuts (pure) -----------------------------------------------------------------------------


def test_15_2_3_due_cluster_cut_falls_between_jobs_4_and_5() -> None:
    # Pinedo p.401: dues 25 -> 33; the lone job 5 (33 -> 51) merges into the next window.
    assert _cuts(load_request("pinedo_15_2_3"), {"min_jobs": 2}) == [(3, "due_gap")]


def test_15_2_3_raw_due_gaps_at_gap_1_cut_after_jobs_4_and_5() -> None:
    # pbar_job = 57 / 9 = 6.33: the gaps 8 (25 -> 33) and 18 (33 -> 51) reach it, 2 and 1 do not.
    cuts = _cuts(load_request("pinedo_15_2_3"), {"min_jobs": 1, "gap": 1.0})
    assert cuts == [(3, "due_gap"), (4, "due_gap")]


def test_15_2_3_short_last_window_merges_into_the_previous() -> None:
    # min_jobs 5: {1..4} joins {5} -> {1..5}; the last {6..9} (4 jobs) joins the previous.
    assert _cuts(load_request("pinedo_15_2_3"), {"min_jobs": 5, "gap": 1.0}) == []


def test_15_2_3_wide_gap_threshold_leaves_one_window() -> None:
    assert _cuts(load_request("pinedo_15_2_3"), {"min_jobs": 1, "gap": 3.0}) == []


def test_release_gap_gives_a_load_cut() -> None:
    cuts = _cuts(_release_gap_request(), {"min_jobs": 1, "gap": 100.0, "eps": 0.01})
    assert cuts == [(1, "load")]


def test_max_jobs_caps_every_window() -> None:
    sizes = _window_sizes(_flat_request(10), {"min_jobs": 1, "max_jobs": 4})
    assert sum(sizes) == 10
    assert max(sizes) <= 4, sizes
    assert min(sizes) >= 1, sizes


def test_cuts_are_ascending_and_inside_the_job_list() -> None:
    request = hmlv_instance(TRAINING_SEEDS[0])
    cuts = _rh().cuts(_index(request), {"min_jobs": 5, "max_jobs": 12})
    positions = [c.after for c in cuts]
    assert positions == sorted(set(positions))
    assert all(0 <= p < len(request.problem.jobs) - 1 for p in positions), positions
    sizes = _window_sizes(request, {"min_jobs": 5, "max_jobs": 12})
    assert max(sizes) <= 12, sizes


# --- Example 15.2.3 solved window by window (p.402) ------------------------------------------


def _book_params(window_solver: str, seam: int) -> dict[str, Any]:
    return {
        "min_jobs": 2,
        "seam": seam,
        "window_solver": window_solver,
        "seam_solver": window_solver,
    }


def _window_solvers(monkeypatch: pytest.MonkeyPatch, which: str) -> str:
    if which == ORACLE:
        _inject(monkeypatch, ORACLE)
    return which


WINDOW_SOLVERS = [pytest.param("ortools", marks=needs_ortools), ORACLE]


@pytest.mark.parametrize("which", WINDOW_SOLVERS)
def test_15_2_3_seams_off_gives_the_naive_value_5(
    which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    window_solver = _window_solvers(monkeypatch, which)
    result = _solve(load_request("pinedo_15_2_3"), _book_params(window_solver, 0))

    assert result.verified, (result.schedule.status, _codes(result))
    assert _book_value(result) == pytest.approx(NAIVE_BOOK_VALUE, abs=1e-9)
    t1 = _tardiness_of_job_1(result)
    assert t1 == pytest.approx(4.0, abs=1e-9)  # window 1 ends at 26 with job 1 last
    expected = NAIVE_BOOK_VALUE + TINY_WEIGHT * t1
    assert result.objective_values[WT] == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("which", WINDOW_SOLVERS)
def test_15_2_3_seams_on_is_at_most_the_naive_value(
    which: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    window_solver = _window_solvers(monkeypatch, which)
    request = load_request("pinedo_15_2_3")
    off = _solve(request, _book_params(window_solver, 0))
    on = _solve(request, _book_params(window_solver, 5))

    assert on.verified, (on.schedule.status, _codes(on))
    assert on.objective_values[WT] <= off.objective_values[WT] + 1e-12
    assert _book_value(on) <= NAIVE_BOOK_VALUE + 1e-9


def test_earlier_windows_are_exact_pins_in_later_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    oracle = _inject(monkeypatch, ORACLE)[ORACLE]
    result = _solve(load_request("pinedo_15_2_3"), _book_params(ORACLE, 0))

    assert result.verified, (result.schedule.status, _codes(result))
    assert len(oracle.calls) == 2, [sorted(_op_ids(c.problem)) for c in oracle.calls]
    first, second = oracle.calls
    assert _op_ids(first.problem) == set(WINDOW_1)  # later windows' jobs are absent
    assert all(_exact_pin(first.problem, o) is None for o in WINDOW_1)
    assert _op_ids(second.problem) == set(WINDOW_1) | set(WINDOW_2)
    final = {a.operation_id: a for a in result.schedule.assignments}
    window_1 = {a.operation_id: a for a in first.returned.assignments}
    for op_id in WINDOW_1:
        assert _exact_pin(second.problem, op_id) == ("M1", window_1[op_id].start), op_id
        assert final[op_id].start == window_1[op_id].start
        assert final[op_id].machine_id == "M1"
    assert all(_exact_pin(second.problem, o) is None for o in WINDOW_2)


def test_pipeline_stage_warm_starts_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    solvers = _inject(monkeypatch, "stage_a", "stage_b")
    params = {"min_jobs": 2, "seam": 0, "window_solver": "stage_a->stage_b"}
    result = _solve(load_request("pinedo_15_2_3"), params)

    assert result.verified, (result.schedule.status, _codes(result))
    a_calls, b_calls = solvers["stage_a"].calls, solvers["stage_b"].calls
    assert len(a_calls) == len(b_calls) == 2
    for a, b in zip(a_calls, b_calls, strict=True):
        assert b.warm_start is not None
        assert set(b.warm_start.assignments) == set(a.returned.assignments)


def test_release_gap_instance_verifies_with_the_default_window_solver() -> None:
    result = _solve(_release_gap_request(), {"min_jobs": 1, "gap": 100.0, "eps": 0.01})
    assert result.verified, (result.schedule.status, _codes(result))
    assert len(result.schedule.assignments) == 4


def test_one_window_when_min_jobs_exceeds_the_job_count() -> None:
    result = _solve(load_request("pinedo_15_2_3"), {"min_jobs": 50})
    assert result.verified, (result.schedule.status, _codes(result))
    assert len(result.schedule.assignments) == 9


# --- window solver (slow, T-152; the gate and 600 x 8 rows live in bench/) ---------------------


@pytest.mark.slow
@needs_ortools
def test_ortools_window_solver_is_called_once_per_window(monkeypatch: pytest.MonkeyPatch) -> None:
    cpsat = importlib.import_module("twinflow.scheduling.algorithms.cpsat")
    calls: list[Problem] = []
    real = cpsat.ORToolsSolver.solve

    def spy(self: Any, problem: Problem, **kwargs: Any) -> Schedule:
        calls.append(problem)
        return real(self, problem, **kwargs)

    monkeypatch.setattr(cpsat.ORToolsSolver, "solve", spy)
    request = hmlv_instance(TRAINING_SEEDS[0])  # 40 jobs x 5 ops
    params = {"window_solver": "ortools", "seam": 0, "min_jobs": 10, "max_jobs": 20}
    windows = len(_rh().cuts(_index(request), params)) + 1

    result = _solve(request, params, budget_seconds=60.0)

    assert result.verified, (result.schedule.status, _codes(result))
    assert windows >= 2
    assert len(calls) == windows, (len(calls), windows)
    for problem in calls:
        free = [o for j in problem.jobs for o in j.operations if _exact_pin(problem, o.id) is None]
        assert len(free) <= 20 * 5, len(free)
