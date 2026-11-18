"""Reactive repair: rush insertion, relaxation and reschedule from state (T-154..T-156;
COMP-020, REQ-112..REQ-114, D-081; Pinedo Sec 18.1 p.482, Sec 16.5 p.448-449, p.549-550).

`twinflow.scheduling.algorithms.reactive` registers `rush_insert` and `reschedule` (family
`reactive`). API and contracts these tests pin:

`rush_insert` (SDD "Rush insertion and relaxation" pseudo-code):

- Needs `context.previous_plan`; without one: UNKNOWN with issue `previous_plan_required`.
- Rush jobs are the jobs with no op in the previous plan. Every other op keeps its machine
  and its order on that machine. Each rush op goes, in route order, to the eligible machine
  and position with the least added setup s(prev, o) + s(o, next) - s(prev, next) (no next:
  s(prev, o); prev None = the machine state), tried in (added, estimated completion, machine
  id, position) order; the first whose decode keeps T_k <= T_k(previous) for every
  protected job k (weight strictly above the rush job's) and, for the last op, C_rush <= due
  is taken.
- Added setup is reported as one informational issue per rush job:
  `ScheduleIssue(code="rush_added_setup", path=<any>, message="... added setup <x> s ...")`,
  `<x>` the seconds (Python `:g` or plain float). `solve()` passes schedule issues through,
  so it appears in `SolveResult.issues`. (The SDD names no field for it; an issue keeps the
  model unchanged.)
- Relaxation (T-155): when no position is acceptable, pos* minimizes sum over protected and
  rush jobs of w_k x increase in T_k (tie: added setup); among the jobs violated at pos*, v =
  min by (w_k, -(due_k - now), job id); `Relaxation(job_id=v, old_due, new_due=C_v(pos*),
  reason)` is appended to `Schedule.relaxations`, v's due becomes new_due, v leaves the
  protected set, and the loop repeats. One `relaxed_due` issue per relaxation whose message
  names the job id. At most |protected| + 1 relaxations.

`reschedule`:

- Warm-starts from the previous sequences (new ops by cheapest insertion, ops on a machine
  that is gone reinserted), adds churn to the objective, runs `params.improver` (default
  `tabu`, by registry name; an unregistered name gives UNKNOWN `subsolver_unavailable`).
- Churn weight: `method_params["reschedule"]["churn_weight"]` (float >= 0, default 1.0); a
  negative or non-numeric value gives UNKNOWN `invalid_method_param` (DEV-15). When the
  profile has `churn_weight` (T-157, portfolio) it is the default; the method param wins.
- `effective_spec(spec, churn_weight, previous_op_count) -> ObjectiveSpec` (pure):
  weighted sum: a `WeightedTerm(kind=churn, weight=churn_weight, scale=previous_op_count)`
  appended; lexicographic: a last `LexicographicLevel(kind=churn)`; epsilon: unchanged.
  `churn_weight == 0` or `previous_op_count == 0` returns the spec unchanged.

The leaf is imported per test, so its absence fails each test rather than collection.
"""

from __future__ import annotations

import functools
import importlib
import re
import time
from collections.abc import Sequence
from types import ModuleType
from typing import Any

import pytest
import test_properties
from bench.hmlv import HELD_OUT_SEEDS, hmlv_instance, scale_instance
from kernel_cases import compile_request, machine, op, request_from

from twinflow.scheduling.model import (
    Assignment,
    EpsilonBound,
    EpsilonConstraint,
    Lexicographic,
    LexicographicLevel,
    ObjectiveKind,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    SolveRequest,
    SolveResult,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.solver import solve
from twinflow.scheduling.verifier import verify

LEAF = "twinflow.scheduling.algorithms.reactive"
WT = ObjectiveKind.WEIGHTED_TARDINESS
CHURN = ObjectiveKind.CHURN
WT_LEX = {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
WT_SUM = {"mode": "weighted_sum", "terms": [{"kind": "weighted_tardiness", "weight": 1.0}]}
ADDED_SETUP = re.compile(r"added setup ([0-9.eE+-]+) s")
MACHINE_DOWN_SEEDS = HELD_OUT_SEEDS[:5]
DOWN_CENTER, DOWN_MACHINE, KEPT_MACHINE = "C0", "C0M1", "C0M0"


def _leaf() -> ModuleType:
    return importlib.import_module(LEAF)


def _run(
    name: str, request: SolveRequest, *, budget: float = 10.0, warm_start: Schedule | None = None
) -> SolveResult:
    return solve(request, solver=get_solver(name), budget_seconds=budget, warm_start=warm_start)


def _verified(result: SolveResult) -> SolveResult:
    assert result.schedule.status in ("FEASIBLE", "OPTIMAL"), result.issues
    assert result.verified, result.issues
    return result


def _sequence(schedule: Schedule, machine_id: str) -> tuple[str, ...]:
    on_machine = [a for a in schedule.assignments if a.machine_id == machine_id]
    return tuple(a.operation_id for a in sorted(on_machine, key=lambda a: (a.start, a.end)))


def _previous_sequence(plan: PreviousPlan, machine_id: str) -> tuple[str, ...]:
    on_machine = [a for a in plan.assignments if a.machine_id == machine_id]
    return tuple(a.operation_id for a in sorted(on_machine, key=lambda a: a.start))


def _completions(schedule: Schedule, request: SolveRequest) -> dict[str, float]:
    job_of = {o.id: job.id for job in request.problem.jobs for o in job.operations}
    done: dict[str, float] = {}
    for a in schedule.assignments:
        job_id = job_of[a.operation_id]
        done[job_id] = max(done.get(job_id, a.end), a.end)
    return done


def _codes(result: SolveResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _plan_of(schedule: Schedule, plan_id: str = "previous") -> PreviousPlan:
    return PreviousPlan(
        plan_id=plan_id,
        problem_digest="previous-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
            for a in schedule.assignments
        ),
    )


def _with(request: SolveRequest, **fields: Any) -> SolveRequest:
    return request.model_copy(update=fields)


# --- T-154 shop: two machines, colour changeovers ---------------------------------------------
#
# Center WC (M1, M2), setup function S: mismatch on `color` 5 s, default 0 (the machine state has
# no colour, so the first op on a machine has no setup). Every run is 10 s.
#   M1: A red (w 6, due 10), B red (class 2, due 100), C blue (w 6, due 35)
#   M2: D blue (w 6, due 10), E blue (class 1, due 100), F red (class 1, due 100)
# Previous plan (left-justified): A 0-10, B 10-20, C 20 [setup to 25] 35; D 0-10, E 10-20,
# F 20 [25] 35. Rush R green, class 4 (w 4), due 60, eligible on M1 and M2.
# Added setup per position (prev, next): M1 (-,A) 5, (A,B) 10, (B,C) 5, (C,-) 5;
# M2 (-,D) 5, (D,E) 10, (E,F) 5, (F,-) 5. In (added, est completion, machine) order:
#   M1 before A (est 10): A ends 25 > 10, protected A gains tardiness -> rejected
#   M2 before D (est 10): D ends 25 > 10 -> rejected
#   M1 between B and C (est 35): C ends 50 > 35 -> rejected
#   M2 between E and F (est 35): D, E untouched, F (unprotected) ends 50 -> accepted.
# So R runs on M2 from 20 (setup to 25) to 35, added setup 5 + 5 - 5 = 5 s, total setup 15.

SHOP_PREVIOUS = {
    "A-1": ("M1", 0.0, 0.0, 10.0),
    "B-1": ("M1", 10.0, 10.0, 20.0),
    "C-1": ("M1", 20.0, 25.0, 35.0),
    "D-1": ("M2", 0.0, 0.0, 10.0),
    "E-1": ("M2", 10.0, 10.0, 20.0),
    "F-1": ("M2", 20.0, 25.0, 35.0),
}
PROTECTED_SHOP_JOBS = ("A", "C", "D")


def _shop_job(job_id: str, color: str, machines: Sequence[str], due: float, **weight: Any) -> Any:
    operation = op(f"{job_id}-1", dict.fromkeys(machines, 10.0), setup_params={"color": color})
    return {"id": job_id, "due": due, "operations": [operation]} | weight


def _shop_request(*, with_rush: bool, with_previous: bool = True) -> SolveRequest:
    jobs = [
        _shop_job("A", "red", ["M1"], 10.0, weight_override=6.0),
        _shop_job("B", "red", ["M1"], 100.0, weight_class=2),
        _shop_job("C", "blue", ["M1"], 35.0, weight_override=6.0),
        _shop_job("D", "blue", ["M2"], 10.0, weight_override=6.0),
        _shop_job("E", "blue", ["M2"], 100.0, weight_class=1),
        _shop_job("F", "red", ["M2"], 100.0, weight_class=1),
    ]
    if with_rush:
        jobs.append(_shop_job("R", "green", ["M1", "M2"], 60.0, weight_class=4))
    problem = {
        "id": "rush-shop",
        "jobs": jobs,
        "machines": [machine("M1"), machine("M2")],
        "work_centers": [{"id": "WC", "setup_function_id": "S"}],
        "setup_functions": [
            {
                "id": "S",
                "terms": [{"kind": "mismatch", "param": "color", "seconds": 5.0}],
                "default_seconds": 0.0,
            }
        ],
    }
    previous = None
    if with_previous:
        previous = {
            "plan_id": "shop-previous",
            "problem_digest": "shop-digest",
            "assignments": [
                {"operation_id": o, "machine_id": m, "start": s}
                for o, (m, s, _, _) in SHOP_PREVIOUS.items()
            ],
        }
    return request_from(problem, profile={"objective": WT_LEX}, previous_plan=previous)


def _shop_previous_schedule() -> Schedule:
    assignments = tuple(
        Assignment(operation_id=o, machine_id=m, start=s, setup_end=se, end=e)
        for o, (m, s, se, e) in SHOP_PREVIOUS.items()
    )
    return Schedule(status="FEASIBLE", assignments=assignments, solver="hand", elapsed_seconds=0.0)


def _added_setup_seconds(result: SolveResult) -> list[float]:
    found = []
    for issue in result.issues:
        if issue.code == "rush_added_setup":
            match = ADDED_SETUP.search(issue.message)
            assert match is not None, issue.message
            found.append(float(match.group(1)))
    return found


# --- registration -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["rush_insert", "reschedule"])
def test_reactive_methods_are_registered_in_family_reactive(name: str) -> None:
    _leaf()
    info = {i.name: i for i in available()}
    assert name in info
    assert info[name].family == "reactive"
    assert info[name].module == LEAF


# --- T-154 rush insertion ---------------------------------------------------------------------


def test_shop_previous_plan_is_the_verified_hand_computed_plan() -> None:
    request = _shop_request(with_rush=False)
    verdict = verify(compile_request(request), (), _shop_previous_schedule(), None)
    assert verdict.valid, verdict.issues


def test_rush_insert_keeps_every_other_sequence_and_takes_the_least_added_setup_position() -> None:
    _leaf()
    request = _shop_request(with_rush=True)
    result = _verified(_run("rush_insert", request))
    schedule = result.schedule
    assert _sequence(schedule, "M1") == ("A-1", "B-1", "C-1")
    assert _sequence(schedule, "M2") == ("D-1", "E-1", "R-1", "F-1")
    rush = next(a for a in schedule.assignments if a.operation_id == "R-1")
    assert (rush.machine_id, rush.start, rush.setup_end, rush.end) == ("M2", 20.0, 25.0, 35.0)
    assert result.objective_values[ObjectiveKind.TOTAL_SETUP] == pytest.approx(15.0)


def test_rush_insert_no_job_weighted_above_the_rush_gains_tardiness() -> None:
    _leaf()
    request = _shop_request(with_rush=True)
    result = _verified(_run("rush_insert", request))
    before = _completions(_shop_previous_schedule(), request)
    after = _completions(result.schedule, request)
    dues = {job.id: job.due or 0.0 for job in request.problem.jobs}
    for job_id in PROTECTED_SHOP_JOBS:
        assert max(0.0, after[job_id] - dues[job_id]) <= max(0.0, before[job_id] - dues[job_id])


def test_rush_insert_reports_the_added_setup_as_an_informational_issue() -> None:
    _leaf()
    result = _verified(_run("rush_insert", _shop_request(with_rush=True)))
    assert _added_setup_seconds(result) == [pytest.approx(5.0)]


def test_rush_insert_feasible_instance_has_no_relaxations() -> None:
    _leaf()
    result = _verified(_run("rush_insert", _shop_request(with_rush=True)))
    assert result.schedule.relaxations == ()
    assert "relaxed_due" not in _codes(result)


def test_rush_insert_without_previous_plan_is_unknown_previous_plan_required() -> None:
    _leaf()
    result = _run("rush_insert", _shop_request(with_rush=True, with_previous=False))
    assert result.schedule.status == "UNKNOWN"
    assert "previous_plan_required" in _codes(result)
    assert not result.verified


@functools.cache
def _scale_rush() -> tuple[SolveRequest, PreviousPlan]:
    base = scale_instance(0)
    previous = _verified(_run("atcs", base, budget=30.0))
    data = base.model_dump(mode="json")
    template = data["problem"]["jobs"][0]
    rush_ops = []
    for k, operation in enumerate(template["operations"]):
        copy = dict(operation, id=f"RUSH-{k}")
        copy["predecessors"] = [{"operation_id": f"RUSH-{k - 1}"}] if k else []
        rush_ops.append(copy)
    horizon = max(a.end for a in previous.schedule.assignments)
    rush = dict(template, id="RUSH", weight_class=4, weight_override=None, due=10 * horizon)
    rush["operations"] = rush_ops
    data["problem"]["jobs"].append(rush)
    plan = _plan_of(previous.schedule, "scale-previous")
    data["previous_plan"] = plan.model_dump(mode="json")
    return SolveRequest.model_validate(data), plan


@pytest.mark.slow
def test_rush_insert_600x8_with_one_8_op_rush_job_under_10_seconds() -> None:
    _leaf()
    request, plan = _scale_rush()
    started = time.perf_counter()
    result = _run("rush_insert", request, budget=10.0)
    elapsed = time.perf_counter() - started
    _verified(result)
    assert elapsed < 10.0, elapsed
    placed = {a.operation_id for a in result.schedule.assignments}
    assert {f"RUSH-{k}" for k in range(8)} <= placed
    for machine_id in {a.machine_id for a in plan.assignments}:
        kept = tuple(o for o in _sequence(result.schedule, machine_id) if not o.startswith("RUSH"))
        assert kept == _previous_sequence(plan, machine_id), machine_id
    assert result.schedule.relaxations == ()


# --- T-155 relaxation -------------------------------------------------------------------------
#
# One machine, no setups, now 0. The previous jobs run back to back from 0 in the listed order.


def _single_machine(
    previous: Sequence[tuple[str, float, float, float]], rush: tuple[str, float, float]
) -> SolveRequest:
    """`previous`: (job id, run, due, weight override); `rush`: (job id, run, due), class 4."""
    jobs = [
        {
            "id": job_id,
            "due": due,
            "weight_override": weight,
            "operations": [op(f"{job_id}-1", {"M1": run})],
        }
        for job_id, run, due, weight in previous
    ]
    rush_id, rush_run, rush_due = rush
    jobs.append(
        {
            "id": rush_id,
            "due": rush_due,
            "weight_class": 4,
            "operations": [op(f"{rush_id}-1", {"M1": rush_run})],
        }
    )
    starts, clock = [], 0.0
    for job_id, run, _, _ in previous:
        starts.append({"operation_id": f"{job_id}-1", "machine_id": "M1", "start": clock})
        clock += run
    problem = {
        "id": "relax",
        "jobs": jobs,
        "machines": [machine("M1")],
        "work_centers": [{"id": "WC"}],
    }
    plan = {"plan_id": "relax-previous", "problem_digest": "relax-digest", "assignments": starts}
    return request_from(problem, profile={"objective": WT_LEX}, previous_plan=plan)


def _relaxed(result: SolveResult) -> list[tuple[str, float, float]]:
    return [(r.job_id, r.old_due, r.new_due) for r in result.schedule.relaxations]


def _assert_relaxations_reported(result: SolveResult, protected: int) -> None:
    relaxations = result.schedule.relaxations
    assert len(relaxations) <= protected + 1
    assert all(r.reason for r in relaxations)
    issues = [i for i in result.issues if i.code == "relaxed_due"]
    assert len(issues) == len(relaxations)
    for relaxation, issue in zip(relaxations, issues, strict=True):
        assert relaxation.job_id in issue.message


@pytest.mark.parametrize(
    ("q_weight", "why"),
    [
        (5.0, "lowest weight"),  # JOB-Q (5) before JOB-P (6)
        (6.0, "most slack"),  # equal weights: JOB-Q due 20 has more slack than JOB-P due 10
    ],
)
def test_rush_insert_relaxes_the_least_valuable_affected_protected_job(
    q_weight: float, why: str
) -> None:
    # Previous: P 0-10 (due 10), Q 10-20 (due 20), X 20-30 (w 1, due 100). Rush R: run 1, due 5.
    # Before P: R 0-1 on time, P 1-11, Q 11-21: both protected jobs gain 1 (cost 6 + w_Q).
    # After P: R ends 11 > 5 (cost 4 x 6 + w_Q); later positions cost 64 and 104. So pos* is
    # first; V = {P, Q}; Q is relaxed 20 -> 21, then P 10 -> 11, and R goes first.
    _leaf()
    request = _single_machine(
        [("JOB-P", 10.0, 10.0, 6.0), ("JOB-Q", 10.0, 20.0, q_weight), ("JOB-X", 10.0, 100.0, 1.0)],
        ("JOB-R", 1.0, 5.0),
    )
    result = _verified(_run("rush_insert", request))
    assert _relaxed(result) == [("JOB-Q", 20.0, 21.0), ("JOB-P", 10.0, 11.0)], why
    assert _sequence(result.schedule, "M1") == ("JOB-R-1", "JOB-P-1", "JOB-Q-1", "JOB-X-1")
    _assert_relaxations_reported(result, protected=2)


def test_rush_insert_relaxes_the_rush_due_when_only_it_must_move() -> None:
    # Previous: P1 0-10 (w 6, due 10), P2 10-20 (w 8, due 20). Rush R: run 10, due 10.
    # Costs: first 6x10 + 8x10 = 140; middle 4x10 + 8x10 = 120; last 4x20 = 80 -> pos* last,
    # V = {R}: R's due 10 -> 30, and R runs last 20-30.
    _leaf()
    request = _single_machine(
        [("JOB-P1", 10.0, 10.0, 6.0), ("JOB-P2", 10.0, 20.0, 8.0)], ("JOB-R", 10.0, 10.0)
    )
    result = _verified(_run("rush_insert", request))
    assert _relaxed(result) == [("JOB-R", 10.0, 30.0)]
    assert _sequence(result.schedule, "M1") == ("JOB-P1-1", "JOB-P2-1", "JOB-R-1")
    _assert_relaxations_reported(result, protected=2)


def test_rush_insert_relaxation_loop_is_bounded_by_protected_count_plus_one() -> None:
    # Five protected jobs, each due exactly at its previous completion, and a rush that is due
    # at 1 and runs 3: no position is free of a violation, whichever job is relaxed first.
    _leaf()
    previous = [(f"JOB-P{k}", 2.0, 2.0 * (k + 1), 5.0 + k) for k in range(5)]
    request = _single_machine(previous, ("JOB-R", 3.0, 1.0))
    result = _verified(_run("rush_insert", request))
    assert result.schedule.relaxations, "an infeasible insertion must relax, never silently"
    _assert_relaxations_reported(result, protected=5)


# --- T-156 reschedule from state --------------------------------------------------------------


def test_effective_spec_weighted_sum_adds_a_churn_term_scaled_by_previous_op_count() -> None:
    spec = WeightedSum(terms=(WeightedTerm(kind=WT, weight=1.0),))
    effective = _leaf().effective_spec(spec, 1.0, 40)
    assert isinstance(effective, WeightedSum)
    assert effective.terms == (
        WeightedTerm(kind=WT, weight=1.0),
        WeightedTerm(kind=CHURN, weight=1.0, scale=40.0),
    )


def test_effective_spec_lexicographic_adds_churn_as_the_last_level() -> None:
    spec = Lexicographic(levels=(LexicographicLevel(kind=WT),))
    effective = _leaf().effective_spec(spec, 2.5, 40)
    assert isinstance(effective, Lexicographic)
    assert [level.kind for level in effective.levels] == [WT, CHURN]


def test_effective_spec_epsilon_constraint_is_unchanged() -> None:
    spec = EpsilonConstraint(primary=WT, bounds=(EpsilonBound(kind=CHURN, upper=3.0),))
    assert _leaf().effective_spec(spec, 1.0, 40) == spec


@pytest.mark.parametrize(
    "spec",
    [
        WeightedSum(terms=(WeightedTerm(kind=WT, weight=1.0),)),
        Lexicographic(levels=(LexicographicLevel(kind=WT),)),
    ],
)
@pytest.mark.parametrize(("weight", "count"), [(0.0, 40), (1.0, 0)])
def test_effective_spec_without_churn_weight_or_previous_ops_has_no_churn_term(
    spec: WeightedSum | Lexicographic, weight: float, count: int
) -> None:
    effective = _leaf().effective_spec(spec, weight, count)
    assert effective == spec
    kinds = (
        [t.kind for t in effective.terms]
        if isinstance(effective, WeightedSum)
        else [lv.kind for lv in effective.levels]
    )
    assert CHURN not in kinds


def _drop_machine(data: dict[str, Any]) -> None:
    """Remove DOWN_MACHINE; an op left without an option gets its run on KEPT_MACHINE."""
    problem = data["problem"]
    problem["machines"] = [m for m in problem["machines"] if m["id"] != DOWN_MACHINE]
    for job in problem["jobs"]:
        for operation in job["operations"]:
            options = operation["options"]
            kept = [o for o in options if o["machine_id"] != DOWN_MACHINE]
            if not kept:
                kept = [dict(options[0], machine_id=KEPT_MACHINE)]
            operation["options"] = kept


def _add_new_jobs(data: dict[str, Any], count: int) -> None:
    jobs = data["problem"]["jobs"]
    for n in range(count):
        template = jobs[n]
        operations = []
        for k, operation in enumerate(template["operations"]):
            copy = dict(operation, id=f"NEW{n}-{k}")
            copy["predecessors"] = [{"operation_id": f"NEW{n}-{k - 1}"}] if k else []
            operations.append(copy)
        jobs.append(dict(template, id=f"NEW{n}", operations=operations))


@functools.cache
def machine_down_scenario(seed: int) -> tuple[SolveRequest, PreviousPlan]:
    """Benchmark machine-down scenario (PLAN T-156).

    A held-out HMLV instance with 2 machines per center is planned by `atcs` (the verified
    previous plan). Then machine C0M1 goes down (removed; its ops keep their previous
    assignments in the previous plan and must be reassigned) and two new jobs arrive (copies
    of J0 and J1 under new ids, absent from the previous plan). The profile objective is the
    weighted sum of weighted tardiness; `reschedule` gets churn weight 1.
    """
    base = hmlv_instance(seed, machines_per_center=2)
    base = _with(
        base,
        profile=base.profile.model_copy(update={"objective": WeightedSum.model_validate(WT_SUM)}),
    )
    previous = _verified(_run("atcs", base))
    plan = _plan_of(previous.schedule, f"machine-down-{seed}")
    data = base.model_dump(mode="json")
    _drop_machine(data)
    _add_new_jobs(data, 2)
    data["previous_plan"] = plan.model_dump(mode="json")
    data["profile"]["method_params"] = {"reschedule": {"churn_weight": 1.0}}
    return SolveRequest.model_validate(data), plan


def _scratch(request: SolveRequest) -> SolveResult:
    """`atcs->tabu` from scratch: ATCS, then tabu warm-started from it."""
    start = _verified(_run("atcs", request))
    return _verified(_run("tabu", request, warm_start=start.schedule))


def test_machine_down_scenario_has_ops_on_the_removed_machine_in_the_previous_plan() -> None:
    request, plan = machine_down_scenario(MACHINE_DOWN_SEEDS[0])
    assert any(a.machine_id == DOWN_MACHINE for a in plan.assignments)
    assert DOWN_MACHINE not in {m.id for m in request.problem.machines}
    previous_ops = {a.operation_id for a in plan.assignments}
    assert any(o.id not in previous_ops for job in request.problem.jobs for o in job.operations)


@pytest.mark.slow
@pytest.mark.parametrize("seed", MACHINE_DOWN_SEEDS)
def test_reschedule_machine_down_moves_fewer_ops_than_scratch_within_10_percent(seed: int) -> None:
    _leaf()
    request, plan = machine_down_scenario(seed)
    repaired = _verified(_run("reschedule", request, budget=10.0))
    scratch = _scratch(request)
    assert repaired.objective_values[CHURN] < scratch.objective_values[CHURN], (
        repaired.objective_values[CHURN],
        scratch.objective_values[CHURN],
    )
    assert repaired.objective_values[WT] <= 1.10 * scratch.objective_values[WT] + 1e-9, (
        repaired.objective_values[WT],
        scratch.objective_values[WT],
    )


def test_reschedule_inserts_new_ops_and_reassigns_ops_of_the_removed_machine() -> None:
    _leaf()
    request, plan = machine_down_scenario(MACHINE_DOWN_SEEDS[0])
    result = _verified(_run("reschedule", request, budget=10.0))
    placed = {a.operation_id: a.machine_id for a in result.schedule.assignments}
    assert DOWN_MACHINE not in placed.values()
    for previous in plan.assignments:
        if previous.machine_id == DOWN_MACHINE:
            assert placed[previous.operation_id] == KEPT_MACHINE
    new_ops = {
        o.id for job in request.problem.jobs if job.id.startswith("NEW") for o in job.operations
    }
    assert new_ops and new_ops <= set(placed)


def test_reschedule_without_previous_plan_still_verifies_or_names_the_gap() -> None:
    # The SDD requires a previous plan only for rush_insert; reschedule without one must not
    # crash: a verified plan, or UNKNOWN with `previous_plan_required`.
    _leaf()
    request, _ = machine_down_scenario(MACHINE_DOWN_SEEDS[0])
    result = _run("reschedule", _with(request, previous_plan=None), budget=5.0)
    assert result.verified or "previous_plan_required" in _codes(result), result.issues
    assert "solver_error" not in _codes(result)


@pytest.mark.parametrize("bad", [-1.0, "heavy"])
def test_reschedule_invalid_churn_weight_is_unknown_invalid_method_param(bad: Any) -> None:
    _leaf()
    request, _ = machine_down_scenario(MACHINE_DOWN_SEEDS[0])
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {"reschedule": {"churn_weight": bad}}
    result = _run("reschedule", SolveRequest.model_validate(data), budget=5.0)
    assert result.schedule.status == "UNKNOWN"
    assert "invalid_method_param" in _codes(result)


def test_reschedule_unregistered_improver_is_unknown_subsolver_unavailable() -> None:
    _leaf()
    request, _ = machine_down_scenario(MACHINE_DOWN_SEEDS[0])
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {"reschedule": {"improver": "no_such_method"}}
    result = _run("reschedule", SolveRequest.model_validate(data), budget=5.0)
    assert result.schedule.status == "UNKNOWN"
    assert "subsolver_unavailable" in _codes(result)


# --- realistic previous plans with frozen pins (property sweep reactive variants) -----------


def _sweep_variant(seed: int) -> SolveRequest:
    """The property sweep's reactive variant: a baseline plan minus a rush job, frozen zone."""
    request = test_properties._sweep_request(seed)
    assert request.previous_plan is not None and request.profile.frozen_zone is not None
    return request


def test_reschedule_keeps_pinned_windows_on_seed_16() -> None:
    # Seed 16: a free op dispatched on M1 ran over the pinned J2-1 (30) and J5-0 (50), so
    # every dispatch, the warm start included, failed `no_feasible_slot`.
    _leaf()
    result = _run("reschedule", _sweep_variant(16), budget=1.0)
    assert result.verified, result.issues


def test_reschedule_falls_back_to_the_previous_sequences_when_dispatch_finds_no_plan() -> None:
    # Seed 361: the atcs dispatch (warm start and improver) finds no plan; the previous
    # sequences with the rush job inserted (rush_insert's construction) verify. (Was seed 61
    # until main's generator drew `finish_unattended`, which shifted every rich seed.)
    _leaf()
    request = _sweep_variant(361)
    assert not _run("atcs", request, budget=1.0).verified
    assert _run("rush_insert", request, budget=1.0).verified
    result = _run("reschedule", request, budget=1.0)
    assert result.verified, result.issues
    assert result.schedule.solver == "reschedule"
