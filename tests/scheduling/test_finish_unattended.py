"""`shift_crossing = "finish_unattended"`: start in working time, then run to the end.

Orchestrator ruling (TF-005 builder gaps): an oven is loaded during a shift and finishes
on its own. The operation must START inside working time (its setup start lies in a
working interval `[s, e)`, half-open) and then setup + run continue without interruption,
ignoring the calendar end: `setup_end = start + setup`, `end = setup_end + run`.

Pinned here:

* the model accepts the value; `forbid` stays the default and `pause` is unchanged;
* the baseline places such an op at the earliest working instant at or after its
  readiness, ends it at `start + setup + run`, and never starts it in non-working time
  (a start late in a shift is fine: only the start must be in working time);
* the verifier accepts that and reports `calendar` for a start outside working time
  (including exactly at an interval end) and `duration` for a wrong run length;
* `Feature.UNATTENDED = "unattended"` is reported for such ops (and not `pause`); the
  baseline supports it, CP-SAT does not, so `solve` with ORToolsSolver refuses with
  `unsupported_feature` and an UNKNOWN schedule;
* a transfer-lot overlap treats it like `pause`: `overlap_requires_forbid`.
"""

from __future__ import annotations

from typing import Any

import pytest

from twinflow.scheduling import (
    Assignment,
    BaselineSolver,
    BatchCapacity,
    Calendar,
    Feature,
    Job,
    Machine,
    Operation,
    Problem,
    ProcessingOption,
    Schedule,
    SetupFunction,
    SolveRequest,
    WorkCenter,
    apply_overrides,
    problem_features,
    solve,
    validate,
    verify,
)

H = 3_600.0
WC = "oven"
# 8-hour shifts on three days: [0, 8h), [24h, 32h), [48h, 56h).
SHIFTS = Calendar(id="shift", intervals=((0.0, 8 * H), (24 * H, 32 * H), (48 * H, 56 * H)))
TEN_HOURS = 10 * H


def _op(op_id: str, seconds: float = TEN_HOURS, **fields: Any) -> Operation:
    data: dict[str, Any] = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": 1.0,
        "options": [
            ProcessingOption(
                machine_id="M", fixed_seconds=seconds, unit_seconds=0.0, preference_cost=0.0
            )
        ],
        "shift_crossing": "finish_unattended",
    }
    return Operation.model_validate(data | fields)


def _request(*jobs: Job, machine: Machine | None = None, **fields: Any) -> SolveRequest:
    data: dict[str, Any] = {
        "id": "p",
        "jobs": jobs,
        "machines": (machine or Machine(id="M", work_center_id=WC, calendar_id="shift"),),
        "work_centers": (WorkCenter(id=WC),),
        "calendars": (SHIFTS,),
    }
    return SolveRequest(schema_version="2.0", problem=Problem.model_validate(data | fields))


def _one_job(*ops: Operation, **fields: Any) -> SolveRequest:
    return _request(Job(id="J", operations=ops), **fields)


def _solved(request: SolveRequest) -> dict[str, Assignment]:
    result = solve(request, solver=BaselineSolver())
    assert result.schedule.status in {"FEASIBLE", "OPTIMAL"}, result.issues
    assert result.verified is True, result.issues
    return {a.operation_id: a for a in result.schedule.assignments}


def _in_working_time(t: float) -> bool:
    return any(start <= t < end for start, end in SHIFTS.intervals)


def _verify(request: SolveRequest, *assignments: Assignment) -> list[str]:
    schedule = Schedule(
        status="FEASIBLE", assignments=assignments, solver="test", elapsed_seconds=0.0
    )
    result = verify(apply_overrides(request), (), schedule)
    return sorted({issue.code for issue in result.issues})


def _a(op_id: str, start: float, setup_end: float, end: float) -> Assignment:
    return Assignment(operation_id=op_id, machine_id="M", start=start, setup_end=setup_end, end=end)


# --- model -------------------------------------------------------------------------------------


def test_operation_accepts_finish_unattended() -> None:
    assert _op("a").shift_crossing == "finish_unattended"


def test_operation_shift_crossing_default_is_still_forbid() -> None:
    option = ProcessingOption(
        machine_id="M", fixed_seconds=1.0, unit_seconds=0.0, preference_cost=0.0
    )
    op = Operation(id="a", work_center_id=WC, quantity=1.0, options=(option,))
    assert op.shift_crossing == "forbid"


def test_operation_rejects_an_unknown_shift_crossing() -> None:
    with pytest.raises(ValueError, match="shift_crossing"):
        _op("a", shift_crossing="finish_attended")


def test_validate_accepts_a_finish_unattended_problem() -> None:
    assert validate(_one_job(_op("a"))) == []


# --- baseline ----------------------------------------------------------------------------------


def test_baseline_runs_a_ten_hour_op_past_an_eight_hour_shift_end() -> None:
    placed = _solved(_one_job(_op("a")))["a"]
    assert (placed.start, placed.setup_end, placed.end) == (0.0, 0.0, TEN_HOURS)


def test_baseline_never_starts_unattended_op_in_non_working_time() -> None:
    # Ready at 9h, inside the break [8h, 24h): it waits for the next shift.
    placed = _solved(_one_job(_op("a", ready_time=9 * H)))["a"]
    assert _in_working_time(placed.start)
    assert (placed.start, placed.end) == (24 * H, 24 * H + TEN_HOURS)


def test_baseline_may_start_unattended_op_late_in_a_shift() -> None:
    # Ready at 7h with one working hour left: only the start must be in working time.
    placed = _solved(_one_job(_op("a", ready_time=7 * H)))["a"]
    assert (placed.start, placed.end) == (7 * H, 7 * H + TEN_HOURS)


def test_baseline_unattended_setup_and_run_are_one_block() -> None:
    request = _one_job(
        _op("a"),
        work_centers=(WorkCenter(id=WC, setup_function_id="s1h"),),
        setup_functions=(SetupFunction(id="s1h", terms=(), default_seconds=H),),
    )
    placed = _solved(request)["a"]
    assert (placed.start, placed.setup_end, placed.end) == (0.0, H, H + TEN_HOURS)


def test_baseline_second_unattended_op_waits_for_the_machine_and_the_next_shift() -> None:
    request = _request(Job(id="J1", operations=(_op("a"),)), Job(id="J2", operations=(_op("b"),)))
    placed = _solved(request)
    first, second = sorted(placed.values(), key=lambda a: a.start)
    assert (first.start, first.end) == (0.0, TEN_HOURS)
    # The machine is busy until 10h, which is a break: the next working instant is 24h.
    assert (second.start, second.end) == (24 * H, 24 * H + TEN_HOURS)


def test_baseline_batches_unattended_oven_loads_verified() -> None:
    oven = Machine(
        id="M",
        work_center_id=WC,
        calendar_id="shift",
        batch=BatchCapacity(capacity=10.0, unit="ea"),
    )
    request = _request(
        Job(id="J1", operations=(_op("a", quantity=2.0, batch_family="F"),)),
        Job(id="J2", operations=(_op("b", quantity=3.0, batch_family="F"),)),
        machine=oven,
    )
    placed = _solved(request)
    for assignment in placed.values():
        assert _in_working_time(assignment.start)
        assert assignment.end == assignment.setup_end + TEN_HOURS


def test_pause_is_unchanged_and_resumes_in_the_next_shift() -> None:
    placed = _solved(_one_job(_op("a", shift_crossing="pause")))["a"]
    # 8h of work in [0, 8h), the last 2h in [24h, 26h).
    assert (placed.start, placed.end) == (0.0, 26 * H)


def test_forbid_still_refuses_an_op_longer_than_any_shift() -> None:
    result = solve(_one_job(_op("a", shift_crossing="forbid")), solver=BaselineSolver())
    assert result.schedule.status == "UNKNOWN"
    assert [issue.code for issue in result.issues] == ["no_feasible_slot"]


# --- verifier ----------------------------------------------------------------------------------


def test_verify_accepts_unattended_run_past_the_shift_end() -> None:
    assert _verify(_one_job(_op("a")), _a("a", 0.0, 0.0, TEN_HOURS)) == []


def test_verify_accepts_unattended_start_late_in_a_shift() -> None:
    assert _verify(_one_job(_op("a")), _a("a", 7 * H, 7 * H, 17 * H)) == []


def test_verify_rejects_unattended_start_in_a_break() -> None:
    assert _verify(_one_job(_op("a")), _a("a", 9 * H, 9 * H, 19 * H)) == ["calendar"]


def test_verify_rejects_unattended_start_exactly_at_a_shift_end() -> None:
    # Intervals are half-open: 8h is the first instant of the break.
    assert _verify(_one_job(_op("a")), _a("a", 8 * H, 8 * H, 18 * H)) == ["calendar"]


def test_verify_rejects_unattended_run_cut_at_the_shift_end() -> None:
    assert _verify(_one_job(_op("a")), _a("a", 0.0, 0.0, 8 * H)) == ["duration"]


def test_verify_rejects_unattended_run_stretched_like_pause() -> None:
    # The pause end (26h) is wrong for an unattended op: it never stops.
    assert _verify(_one_job(_op("a")), _a("a", 0.0, 0.0, 26 * H)) == ["duration"]


# --- features and solvers ----------------------------------------------------------------------


def test_feature_unattended_value() -> None:
    assert Feature.UNATTENDED.value == "unattended"


def test_problem_features_reports_unattended_and_not_pause() -> None:
    features = problem_features(_one_job(_op("a")).problem)
    assert Feature.UNATTENDED in features
    assert Feature.PAUSE not in features


def test_problem_features_skips_unattended_on_done_ops() -> None:
    done = _op("a", status={"state": "done"})
    assert Feature.UNATTENDED not in problem_features(_one_job(done).problem)


def test_baseline_declares_unattended_support() -> None:
    assert Feature.UNATTENDED in BaselineSolver().capabilities


def test_solve_reports_unattended_feature() -> None:
    result = solve(_one_job(_op("a")), solver=BaselineSolver())
    assert "unattended" in result.features


def test_ortools_refuses_unattended_with_unsupported_feature() -> None:
    pytest.importorskip("ortools")
    from twinflow.scheduling.algorithms.cpsat import ORToolsSolver

    solver = ORToolsSolver()
    assert Feature.UNATTENDED not in solver.capabilities
    result = solve(_one_job(_op("a")), solver=solver, budget_seconds=5.0)
    assert result.schedule.status == "UNKNOWN"
    assert result.verified is False
    unsupported = [issue for issue in result.issues if issue.code == "unsupported_feature"]
    assert unsupported and any("unattended" in issue.message for issue in unsupported)


# --- overlap -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pred_crossing", "succ_crossing"),
    [("finish_unattended", "forbid"), ("forbid", "finish_unattended")],
)
def test_validate_overlap_with_unattended_op_requires_forbid(
    pred_crossing: str, succ_crossing: str
) -> None:
    pred = _op("a", quantity=10.0, shift_crossing=pred_crossing)
    succ = _op(
        "b",
        quantity=10.0,
        shift_crossing=succ_crossing,
        predecessors=[{"operation_id": "a", "overlap_quantity": 2.0}],
    )
    issues = validate(_one_job(pred, succ))
    assert [(issue.code, issue.path) for issue in issues] == [
        (
            "overlap_requires_forbid",
            "problem.jobs[0].operations[1].predecessors[0].overlap_quantity",
        )
    ]


# --- every registered solver (TF-003 merge) ----------------------------------------------------


def _registered_names() -> list[str]:
    from twinflow.scheduling import available_solvers

    return [info.name for info in available_solvers()]


@pytest.mark.parametrize("name", _registered_names())
def test_every_registered_solver_verifies_or_refuses_unattended_ops(name: str) -> None:
    # Ops longer than a shift on one calendared oven: a solver either verifies or refuses
    # by name (ortools: unsupported_feature); it never crashes or returns a bad plan.
    from twinflow.scheduling import get_solver
    from twinflow.scheduling.seam import OptionalDependencyError

    try:
        solver = get_solver(name)
    except OptionalDependencyError:
        pytest.skip(f"{name} needs an optional extra")
    jobs = [
        Job(id=f"J{k}", due=30 * H, operations=(_op(f"J{k}:10", seconds=(3 + k) * H),))
        for k in range(4)
    ]

    result = solve(_request(*jobs), solver=solver, budget_seconds=5.0)

    codes = [issue.code for issue in result.issues]
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified is True, (name, result.issues)
    else:
        assert codes, (name, result.schedule.status)
        assert "solver_error" not in codes, (name, result.issues)
