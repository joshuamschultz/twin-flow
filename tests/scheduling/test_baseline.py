"""Tests for the baseline list scheduler (T-102, COMP-009, REQ-075, REQ-079).

Every FEASIBLE answer is checked by the independent verifier through `solve()`; tests
that call `BaselineSolver.solve` directly look at placement order and refusal codes.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from oracle import brute_force

from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.model import (
    Assignment,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    Operation,
    Problem,
    Schedule,
    SolveRequest,
    SolveResult,
    WorkCenter,
)
from twinflow.scheduling.seam import Feature, SolveContext
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate

FIXTURES = Path(__file__).parent / "fixtures"
WC = "WC"
MAKESPAN = Lexicographic(levels=(LexicographicLevel(kind=ObjectiveKind.MAKESPAN),))
SMOKE_SEEDS = range(30)


# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, *options: tuple[str, float], **fields: Any) -> Operation:
    """An operation of quantity 1 with `(machine_id, fixed_seconds)` options (M1, 5 s default)."""
    data = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": 1.0,
        "options": [
            {"machine_id": m, "fixed_seconds": s, "unit_seconds": 0.0, "preference_cost": 0.0}
            for m, s in options or (("M1", 5.0),)
        ],
    }
    return Operation.model_validate(data | fields)


def _machine(machine_id: str = "M1", **fields: Any) -> Machine:
    return Machine.model_validate({"id": machine_id, "work_center_id": WC} | fields)


def _problem(jobs: Sequence[Job], machines: Sequence[Machine] = (), **fields: Any) -> Problem:
    data: dict[str, Any] = {
        "id": "p",
        "jobs": tuple(jobs),
        "machines": tuple(machines) or (_machine(),),
        "work_centers": (WorkCenter(id=WC),),
    }
    return Problem.model_validate(data | fields)


def _one_job(*operations: Operation, **job_fields: Any) -> list[Job]:
    return [Job.model_validate({"id": "J", "operations": operations} | job_fields)]


def _request(problem: Problem, **fields: Any) -> SolveRequest:
    return SolveRequest.model_validate({"schema_version": "2.0", "problem": problem} | fields)


def _context(budget_seconds: float) -> SolveContext:
    deadline = time.monotonic() + budget_seconds
    return SolveContext(
        previous_plan=None, churn_tolerance=0.0, deadline=deadline, warm_start=None, params={}
    )


def _direct(problem: Problem) -> Schedule:
    return BaselineSolver().solve(
        problem, objective=MAKESPAN, budget_seconds=10.0, context=_context(10.0)
    )


def _by_op(schedule: Schedule) -> dict[str, Assignment]:
    return {a.operation_id: a for a in schedule.assignments}


def _codes(schedule: Schedule) -> list[str]:
    return [issue.code for issue in schedule.issues]


def _verified(problem: Problem) -> SolveResult:
    result = solve(_request(problem))
    assert result.schedule.status == "FEASIBLE", result.issues
    assert result.verified, result.issues
    return result


def _fixture(name: str) -> SolveRequest:
    return SolveRequest.model_validate_json((FIXTURES / name).read_text())


# --- seam ----------------------------------------------------------------------------------


def test_capabilities_are_every_feature_except_restart() -> None:
    solver = BaselineSolver()

    assert solver.name == "baseline"
    assert solver.capabilities == frozenset(Feature) - {Feature.RESTART}


def test_restart_machine_is_refused_by_the_default_solver() -> None:
    restart = {"idle_threshold_seconds": 60.0, "warmup_seconds": 10.0}
    problem = _problem(_one_job(_op("A")), [_machine(restart=restart)])

    result = solve(_request(problem))

    assert result.schedule.status == "UNKNOWN"
    assert result.schedule.solver == "baseline"
    assert [i.code for i in result.issues] == ["unsupported_feature"]
    assert "restart" in result.issues[0].message


# --- placement order -----------------------------------------------------------------------


def test_in_progress_op_is_placed_at_now_before_pending_work() -> None:
    running = _op(
        "Z", status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 7.0}
    )
    jobs = [*_one_job(_op("A")), Job(id="K", operations=(running,))]
    problem = _problem(jobs, now=100.0)

    placed = _by_op(_direct(problem))

    assert (placed["Z"].start, placed["Z"].setup_end, placed["Z"].end) == (100.0, 100.0, 107.0)
    assert placed["A"].start == 107.0
    _verified(problem)


def test_pinned_op_is_placed_before_unpinned_ops() -> None:
    pinned = _op("B", pin={"earliest_start": 0.0, "latest_start": 0.0})
    problem = _problem([*_one_job(_op("A")), Job(id="K", operations=(pinned,))])

    placed = _by_op(_direct(problem))

    assert placed["B"].start == 0.0
    assert placed["A"].start == 5.0
    _verified(problem)


def test_ready_ops_are_placed_by_ready_time_then_id() -> None:
    jobs = [
        Job(id="J1", release=3.0, operations=(_op("A"),)),
        Job(id="J2", operations=(_op("B"),)),
        Job(id="J3", operations=(_op("C"),)),
    ]
    placed = _by_op(_direct(_problem(jobs)))

    assert [placed[op].start for op in "BCA"] == [0.0, 5.0, 10.0]


# --- option choice -------------------------------------------------------------------------


def test_option_with_earliest_completion_wins() -> None:
    problem = _problem(
        _one_job(_op("A", ("M1", 10.0), ("M2", 4.0))), [_machine("M1"), _machine("M2")]
    )

    assert _by_op(_direct(problem))["A"].machine_id == "M2"
    _verified(problem)


def test_completion_tie_goes_to_the_lowest_machine_id() -> None:
    problem = _problem(
        _one_job(_op("A", ("M2", 5.0), ("M1", 5.0))), [_machine("M2"), _machine("M1")]
    )

    assert _by_op(_direct(problem))["A"].machine_id == "M1"


def test_pinned_machine_restricts_the_options() -> None:
    op = _op("A", ("M1", 10.0), ("M2", 4.0), pin={"machine_id": "M1"})
    problem = _problem(_one_job(op), [_machine("M1"), _machine("M2")])

    assert _by_op(_direct(problem))["A"].machine_id == "M1"
    _verified(problem)


# --- refusals: UNKNOWN, never INFEASIBLE -----------------------------------------------------


def test_max_lag_that_cannot_be_met_returns_unknown_max_lag_unmet() -> None:
    blocker = _op(
        "Z",
        ("M2", 5.0),
        status={"state": "in_progress", "machine_id": "M2", "remaining_seconds": 100.0},
    )
    follower = _op("B", ("M2", 5.0), predecessors=[{"operation_id": "A", "max_lag": 0.0}])
    jobs = [*_one_job(_op("A"), follower), Job(id="K", operations=(blocker,))]
    problem = _problem(jobs, [_machine("M1"), _machine("M2")])

    schedule = _direct(problem)

    assert schedule.status == "UNKNOWN"
    assert _codes(schedule) == ["max_lag_unmet"]
    assert schedule.assignments == ()


def test_op_longer_than_every_shift_returns_unknown_no_feasible_slot() -> None:
    calendar = {"id": "C", "intervals": [(0.0, 10.0), (20.0, 30.0)]}
    problem = _problem(
        _one_job(_op("A", ("M1", 15.0))), [_machine(calendar_id="C")], calendars=[calendar]
    )

    schedule = _direct(problem)

    assert schedule.status == "UNKNOWN"
    assert _codes(schedule) == ["no_feasible_slot"]


def test_pin_window_that_is_already_taken_returns_unknown_no_feasible_slot() -> None:
    pin = {"earliest_start": 0.0, "latest_start": 0.0}
    jobs = [
        Job(id="J1", operations=(_op("A", pin=pin),)),
        Job(id="J2", operations=(_op("B", pin=pin),)),
    ]

    result = solve(_request(_problem(jobs)))

    assert result.schedule.status == "UNKNOWN"
    assert [i.code for i in result.issues] == ["no_feasible_slot"]


def test_two_in_progress_ops_on_one_machine_return_unknown_no_feasible_slot() -> None:
    status = {"state": "in_progress", "machine_id": "M1", "remaining_seconds": 5.0}
    jobs = [
        Job(id="J1", operations=(_op("A", status=status),)),
        Job(id="J2", operations=(_op("B", status=status),)),
    ]

    schedule = _direct(_problem(jobs))

    assert schedule.status == "UNKNOWN"
    assert _codes(schedule) == ["no_feasible_slot"]


def test_in_progress_op_pinned_to_another_machine_is_refused_by_name() -> None:
    running = _op(
        "p",
        ("M1", 5.0),
        ("M2", 5.0),
        pin={"machine_id": "M2"},
        status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 4.0},
    )
    problem = _problem(_one_job(running), (_machine("M1"), _machine("M2")))

    result = solve(_request(problem))

    assert result.schedule.status == "UNKNOWN"
    assert [issue.code for issue in result.issues] == ["in_progress_machine"]


def test_frozen_zone_leaves_an_in_progress_op_at_now_instead_of_refusing_it() -> None:
    running = _op(
        "p", status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 4.0}
    )
    previous = {
        "plan_id": "prev",
        "problem_digest": "d",
        "assignments": [{"operation_id": "p", "machine_id": "M1", "start": 3.0}],
    }
    profile = {"frozen_zone": {"until": 50.0, "tolerance": 0.0}}
    request = _request(
        _problem(_one_job(running), now=10.0), profile=profile, previous_plan=previous
    )

    result = solve(request)

    assert result.verified, result.issues
    assert _by_op(result.schedule)["p"].start == 10.0


def test_successor_of_a_held_job_is_refused_with_held_predecessor() -> None:
    jobs = [
        Job(id="J", status="held", operations=(_op("A"),)),
        Job(id="K", operations=(_op("B", predecessors=[{"operation_id": "A"}]),)),
    ]

    result = solve(_request(_problem(jobs)))

    assert result.schedule.status == "UNKNOWN"
    assert [issue.code for issue in result.issues] == ["held_predecessor"]


@pytest.mark.parametrize(
    ("problem_end", "profile"),
    [(6.0, {}), (None, {"horizon_end": 6.0})],
)
def test_plan_ending_past_the_horizon_returns_unknown_horizon_exceeded(
    problem_end: float | None, profile: dict[str, Any]
) -> None:
    jobs = [Job(id="J1", operations=(_op("a"),)), Job(id="J2", operations=(_op("b"),))]
    problem = _problem(jobs, horizon_end=problem_end)

    result = solve(_request(problem, profile=profile))

    assert result.schedule.status == "UNKNOWN"
    assert [issue.code for issue in result.issues] == ["horizon_exceeded"]


def test_pass_that_outlives_its_budget_returns_unknown_time_limit() -> None:
    jobs = [Job(id=f"J{k}", operations=(_op(f"op{k:05}"),)) for k in range(4000)]
    problem = _problem(jobs)

    schedule = BaselineSolver().solve(
        problem, objective=MAKESPAN, budget_seconds=0.05, context=_context(0.05)
    )

    assert schedule.status == "UNKNOWN"
    assert _codes(schedule) == ["time_limit"]
    assert schedule.assignments == ()
    assert schedule.elapsed_seconds < 1.0


def test_same_input_gives_identical_output() -> None:
    request = _fixture("pinedo_7_1_1.json")

    first, second = solve(request), solve(request)

    assert first.schedule.assignments == second.schedule.assignments
    assert first.objective_values == second.objective_values


# --- Pinedo fixtures -----------------------------------------------------------------------


def test_pinedo_7_1_1_verifies_with_makespan_at_least_the_optimum() -> None:
    request = _fixture("pinedo_7_1_1.json")
    optimum, _ = brute_force(request.problem, ObjectiveKind.MAKESPAN)

    result = solve(request)

    assert result.verified, result.issues
    assert result.schedule.status == "FEASIBLE"
    assert result.objective_values[ObjectiveKind.MAKESPAN] >= optimum == 28.0
    assert result.schedule.primary_value == result.objective_values[ObjectiveKind.MAKESPAN]


def test_pinedo_14_2_1_verifies_with_weighted_tardiness_at_least_98() -> None:
    result = solve(_fixture("pinedo_14_2_1.json"))

    assert result.verified, result.issues
    assert result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS] >= 98.0
    assert (
        result.schedule.primary_value == result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS]
    )


# --- material gate and transfer lots ---------------------------------------------------------


def test_detached_setup_finishes_no_later_than_attached_with_a_release() -> None:
    setup = {"id": "S", "terms": [], "default_seconds": 5.0}

    def finish(detached: bool) -> float:
        op = _op("A", setup_detached=detached)
        problem = _problem(
            _one_job(op, release=10.0),
            [_machine(setup_function_id="S")],
            setup_functions=[setup],
        )
        result = _verified(problem)
        return _by_op(result.schedule)["A"].end

    assert finish(detached=True) == 15.0
    assert finish(detached=False) == 20.0


def _lot(op_id: str, machine: str, unit: float, **fields: Any) -> Operation:
    """A lot of 10 units at `unit` seconds each, no fixed time."""
    option = {"machine_id": machine, "fixed_seconds": 0.0, "unit_seconds": unit}
    data = {"id": op_id, "work_center_id": WC, "quantity": 10.0}
    return Operation.model_validate(
        data | {"options": [option | {"preference_cost": 0.0}]} | fields
    )


def _transfer_problem(successor_unit: float) -> Problem:
    """A then B on two machines, B may start once 2 units of A are done."""
    first = _lot("A", "M1", 1.0)
    link = {"operation_id": "A", "overlap_quantity": 2.0}
    second = _lot("B", "M2", successor_unit, predecessors=[link])
    return _problem(_one_job(first, second), [_machine("M1"), _machine("M2")])


def test_transfer_lot_lets_the_successor_start_after_the_first_lot() -> None:
    placed = _by_op(_verified(_transfer_problem(successor_unit=1.0)).schedule)

    assert (placed["B"].start, placed["B"].end) == (2.0, 12.0)


def test_faster_successor_waits_to_end_after_the_last_lot() -> None:
    placed = _by_op(_verified(_transfer_problem(successor_unit=0.5)).schedule)

    # end >= A.end + q * B.unit = 10 + 2 * 0.5, so B (5 s of run) starts at 6.
    assert (placed["B"].start, placed["B"].end) == (6.0, 11.0)


# --- zero-length operations (TF-002 review) -------------------------------------------------


def test_zero_length_op_between_two_ops_at_the_same_instant_verifies() -> None:
    jobs = [
        Job(id="J1", operations=(_op("op1", ("M1", 10.0)),)),
        Job(id="J3", operations=(_op("op3", ("M1", 0.0)),)),
        Job(id="J2", operations=(_op("op2", ("M1", 10.0)),), release=5.0),
    ]

    result = _verified(_problem(jobs))

    placed = _by_op(result.schedule)
    assert (placed["op3"].start, placed["op3"].end) == (10.0, 10.0)


def test_zero_length_op_released_into_a_closed_gap_is_placed_and_verifies() -> None:
    jobs = [
        Job(id="J1", operations=(_op("op1", ("M1", 10.0)),)),
        Job(id="J2", operations=(_op("op2", ("M1", 10.0)),), release=5.0),
        Job(id="J3", operations=(_op("op3", ("M1", 0.0)),), release=7.0),
    ]

    result = _verified(_problem(jobs))

    placed = _by_op(result.schedule)
    assert placed["op3"].start == placed["op3"].end == 10.0


# --- operators and batches -------------------------------------------------------------------


def test_operator_pool_of_one_serializes_two_machines() -> None:
    need = [{"pool_id": "P", "quantity": 1, "phase": "both"}]
    jobs = [
        Job(id="J1", operations=(_op("A", ("M1", 5.0), operator_needs=need),)),
        Job(id="J2", operations=(_op("B", ("M2", 5.0), operator_needs=need),)),
    ]
    problem = _problem(
        jobs, [_machine("M1"), _machine("M2")], operator_pools=[{"id": "P", "size": 1}]
    )

    placed = _by_op(_verified(problem).schedule)

    assert sorted((placed["A"].start, placed["B"].start)) == [0.0, 5.0]


def test_same_family_ops_share_one_batch() -> None:
    jobs = [
        Job(id="J1", operations=(_op("A", ("B1", 10.0), batch_family="F"),)),
        Job(id="J2", operations=(_op("B", ("B1", 10.0), batch_family="F"),)),
    ]
    oven = _machine("B1", batch={"capacity": 2.0, "unit": "pc"})

    placed = _by_op(_verified(_problem(jobs, [oven])).schedule)

    assert placed["A"].batch_id == placed["B"].batch_id is not None
    assert placed["A"].start == placed["B"].start == 0.0


# --- seeded smoke loop -----------------------------------------------------------------------


_MACHINES = ("M1", "M2", "M3")
_CALENDAR = [(k * 100.0, k * 100.0 + 80.0) for k in range(200)]
_POOL_CALENDAR = [(k * 50.0, k * 50.0 + 45.0) for k in range(400)]


def _int(rng: np.random.Generator, low: int, high: int) -> int:
    """A uniform integer in [low, high], both ends included."""
    return int(rng.integers(low, high + 1))


def _pick(rng: np.random.Generator, choices: Sequence[Any]) -> Any:
    return choices[int(rng.integers(len(choices)))]


def _random_option(rng: np.random.Generator, machine: str) -> dict[str, Any]:
    fixed, unit = float(_int(rng, 1, 15)), float(_pick(rng, (0, 1, 2)))
    return {
        "machine_id": machine,
        "fixed_seconds": fixed,
        "unit_seconds": unit,
        "preference_cost": 0.0,
    }


def _random_link(
    rng: np.random.Generator, pred: dict[str, Any], op: dict[str, Any]
) -> dict[str, Any]:
    link: dict[str, Any] = {"operation_id": pred["id"], "min_lag": float(_pick(rng, (0, 0, 3)))}
    if rng.random() < 0.2:
        link["max_lag"] = link["min_lag"] + 400.0
    both_forbid = pred["shift_crossing"] == op["shift_crossing"] == "forbid"
    if both_forbid and rng.random() < 0.3:
        link["overlap_quantity"] = float(_int(rng, 1, int(pred["quantity"])))
    return link


def _random_operation(rng: np.random.Generator, op_id: str) -> dict[str, Any]:
    machines = [str(m) for m in rng.permutation(_MACHINES)[: _int(rng, 1, 3)]]
    op: dict[str, Any] = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": float(_int(rng, 1, 4)),
        "options": [_random_option(rng, m) for m in machines],
        "setup_params": {"w": float(_int(rng, 0, 5)), "c": _pick(rng, "xy")},
        "setup_detached": rng.random() < 0.3,
        "shift_crossing": _pick(rng, ("forbid", "pause")),
        "ready_time": float(_pick(rng, (0, 0, 20))),
    }
    if rng.random() < 0.3:
        op["operator_needs"] = [
            {"pool_id": "P", "quantity": 1, "phase": _pick(rng, ("setup", "run", "both"))}
        ]
    if rng.random() < 0.15:
        op["pin"] = _pick(
            rng, ({"machine_id": machines[0]}, {"earliest_start": float(_int(rng, 0, 100))})
        )
    return op


def _random_job(rng: np.random.Generator, j: int) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for k in range(_int(rng, 1, 4)):
        op = _random_operation(rng, f"J{j}-{k}")
        if operations:
            op["predecessors"] = [_random_link(rng, operations[-1], op)]
        operations.append(op)
    if j < len(_MACHINES) and rng.random() < 0.3:
        _start_running(rng, operations[0], _MACHINES[j])
    return {"id": f"J{j}", "release": float(_pick(rng, (0, 0, 15))), "operations": operations}


def _start_running(rng: np.random.Generator, op: dict[str, Any], machine: str) -> None:
    """Make `op` in progress on `machine` (job j runs on the j-th machine)."""
    if machine not in {option["machine_id"] for option in op["options"]}:
        op["options"].append(_random_option(rng, machine))
    remaining = float(_int(rng, 1, 30))
    op["status"] = {"state": "in_progress", "machine_id": machine, "remaining_seconds": remaining}
    op.pop("pin", None)
    op.pop("operator_needs", None)


def _batch_job(rng: np.random.Generator, j: int) -> dict[str, Any]:
    option = {
        "machine_id": "B1",
        "fixed_seconds": 10.0,
        "unit_seconds": 0.0,
        "preference_cost": 0.0,
    }
    op = {
        "id": f"J{j}-0",
        "work_center_id": WC,
        "quantity": float(_int(rng, 1, 2)),
        "options": [option],
        "batch_family": _pick(rng, "FG"),
    }
    return {"id": f"J{j}", "release": float(_pick(rng, (0, 5))), "operations": [op]}


def _random_problem(seed: int) -> Problem:
    rng = np.random.default_rng(seed)
    jobs = [_random_job(rng, j) for j in range(_int(rng, 2, 5))]
    jobs += [_batch_job(rng, j) for j in range(len(jobs), len(jobs) + _int(rng, 0, 3))]
    machines: list[dict[str, Any]] = [
        {
            "id": m,
            "work_center_id": WC,
            "calendar_id": _pick(rng, (None, "C")),
            "state": {
                "available_from": float(_pick(rng, (0, 0, 30))),
                "last_setup_params": {"w": float(_int(rng, 0, 5)), "c": "x"},
            },
        }
        for m in _MACHINES
    ]
    machines.append({"id": "B1", "work_center_id": WC, "batch": {"capacity": 3.0, "unit": "pc"}})
    pool_calendar = _pick(rng, (None, "PC"))
    return Problem.model_validate(
        {
            "id": f"smoke-{seed}",
            "jobs": jobs,
            "machines": machines,
            "work_centers": [{"id": WC, "setup_function_id": "S"}],
            "calendars": [
                {"id": "C", "intervals": _CALENDAR},
                {"id": "PC", "intervals": _POOL_CALENDAR},
            ],
            "operator_pools": [{"id": "P", "size": 1, "calendar_id": pool_calendar}],
            "setup_functions": [
                {
                    "id": "S",
                    "terms": [
                        {"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0},
                        {"kind": "mismatch", "param": "c", "seconds": 3.0},
                    ],
                }
            ],
        }
    )


def test_smoke_every_feasible_random_problem_verifies() -> None:
    feasible = 0
    for seed in SMOKE_SEEDS:
        request = _request(_random_problem(seed))
        assert validate(request) == [], f"seed {seed}: generator built an invalid problem"
        result = solve(request)
        assert result.schedule.status != "INFEASIBLE", f"seed {seed}"
        if result.schedule.status == "FEASIBLE":
            assert result.verified, f"seed {seed}: {result.issues}"
            feasible += 1
    assert feasible >= len(SMOKE_SEEDS) // 2


@pytest.mark.parametrize("seed", [3, 17])
def test_smoke_is_deterministic(seed: int) -> None:
    problem = _random_problem(seed)

    assert _direct(problem).assignments == _direct(problem).assignments
