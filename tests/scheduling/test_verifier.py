"""Mutation tests for the independent verifier (T-097, COMP-007, COMP-012).

Every base case is a schedule that is valid by construction: the two Pinedo fixtures
use the brute-force oracle; the small hand problems (calendars, operators, batches, lags,
overlap, pins, shop-floor status, holds, splits) have their assignments written out with
times computed from the SDD Data Model formulas. Each unmutated base case must verify.
Each mutation breaks exactly one constraint family and must yield exactly that family's
issue code from COMP-007, no more and no less.

Requirements: REQ-074, REQ-077.
"""

from __future__ import annotations

import ast
import functools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mutations import (
    add,
    drop,
    join_batch,
    move_to_machine,
    replace,
    reversed_order,
    shift,
    shorten_run,
    shorten_setup,
    with_nonfinite,
    with_primary,
)
from oracle import brute_force

from twinflow.scheduling import verifier
from twinflow.scheduling.model import (
    Assignment,
    BatchCapacity,
    Calendar,
    CompiledProblem,
    ExcludeMachineOverride,
    FrozenZone,
    HoldOverride,
    Job,
    LockOverride,
    Machine,
    MachineState,
    ObjectiveKind,
    Operation,
    OperatorPool,
    Override,
    PreferMachineOverride,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    ProcessingOption,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    SplitOverride,
    VerificationResult,
    WorkCenter,
)
from twinflow.scheduling.overrides import apply_overrides

FIXTURES = Path(__file__).parent / "fixtures"
WC = "WC"
SETUP_3 = SetupFunction(id="setup-3", terms=(), default_seconds=3.0)
SETUP_2 = SetupFunction(id="setup-2", terms=(), default_seconds=2.0)
Mutation = Callable[[Schedule], Schedule]


# --- builders ------------------------------------------------------------------------------


def _option(machine_id: str, fixed: float = 5.0, unit: float = 0.0) -> ProcessingOption:
    return ProcessingOption(
        machine_id=machine_id, fixed_seconds=fixed, unit_seconds=unit, preference_cost=0.0
    )


def _op(op_id: str, *machines: str, seconds: float = 5.0, **fields: Any) -> Operation:
    """An operation of quantity 1 with `seconds` of run on each listed machine."""
    data: dict[str, Any] = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": 1.0,
        "options": [_option(m, seconds) for m in machines],
    }
    return Operation.model_validate(data | fields)


def _after(op_id: str, **link: float) -> dict[str, Any]:
    """`predecessors` field value: one link from `op_id` with optional lag fields."""
    return {"predecessors": [{"operation_id": op_id, **link}]}


def _job(job_id: str, *operations: Operation, **fields: Any) -> Job:
    return Job.model_validate({"id": job_id, "operations": operations} | fields)


def _machine(machine_id: str, **fields: Any) -> Machine:
    return Machine.model_validate({"id": machine_id, "work_center_id": WC} | fields)


def _problem(jobs: Sequence[Job], machines: Sequence[Machine], **fields: Any) -> Problem:
    data: dict[str, Any] = {
        "id": "p",
        "jobs": tuple(jobs),
        "machines": tuple(machines),
        "work_centers": (WorkCenter(id=WC),),
    }
    return Problem.model_validate(data | fields)


def _compile(problem: Problem, overrides: Sequence[Override] = ()) -> CompiledProblem:
    request = SolveRequest(schema_version="2.0", problem=problem, overrides=tuple(overrides))
    return apply_overrides(request)


def _a(
    op_id: str,
    machine_id: str,
    start: float,
    setup_end: float,
    end: float,
    batch_id: str | None = None,
) -> Assignment:
    return Assignment(
        operation_id=op_id,
        machine_id=machine_id,
        start=start,
        setup_end=setup_end,
        end=end,
        batch_id=batch_id,
    )


def _schedule(*assignments: Assignment) -> Schedule:
    return Schedule(status="FEASIBLE", assignments=assignments, solver="hand", elapsed_seconds=0.0)


def _meta() -> dict[str, str]:
    return {"fact_ref": "test:fact", "reason": "test reason"}


@dataclass(frozen=True)
class Case:
    """A compiled problem, the raw overrides it came from, and a valid schedule for it."""

    compiled: CompiledProblem
    schedule: Schedule
    raw_overrides: tuple[Override, ...] = ()


def _check(case: Case, schedule: Schedule | None = None) -> VerificationResult:
    """Verify `schedule` (default: the case's own) and check `valid` agrees with issues."""
    result = verifier.verify(case.compiled, case.raw_overrides, schedule or case.schedule)
    assert result.valid is (not result.issues), result
    return result


def _codes(result: VerificationResult) -> set[str]:
    return {issue.code for issue in result.issues}


def _only(result: VerificationResult, code: str) -> list[ScheduleIssue]:
    """Assert the result holds issues of exactly one code; return them."""
    assert not result.valid
    assert _codes(result) == {code}, result.issues
    return list(result.issues)


def _names(issues: Sequence[ScheduleIssue], operation_id: str) -> bool:
    return any(operation_id in f"{i.path} {i.message}" for i in issues)


# --- base cases: fixtures ------------------------------------------------------------------


def _load(name: str) -> SolveRequest:
    return SolveRequest.model_validate_json((FIXTURES / name).read_text())


@functools.cache
def _fixture_case(name: str, kind: ObjectiveKind) -> Case:
    """Oracle-optimal schedule for a fixture; `primary_value` holds the oracle optimum."""
    request = _load(name)
    _, schedule = brute_force(request.problem, kind, request.profile)
    return Case(compiled=apply_overrides(request), schedule=schedule)


def _pinedo_7_1_1() -> Case:
    case = _fixture_case("pinedo_7_1_1.json", ObjectiveKind.MAKESPAN)
    return Case(case.compiled, with_primary(case.schedule, None))


def _pinedo_14_2_1() -> Case:
    case = _fixture_case("pinedo_14_2_1.json", ObjectiveKind.WEIGHTED_TARDINESS)
    return Case(case.compiled, with_primary(case.schedule, None))


# --- base cases: hand problems ---------------------------------------------------------------


def _setups(*, detached: bool) -> Case:
    """p on A [0, 10); s on B (setup 3) after p. Attached: s at 10; detached: run at 10."""
    problem = _problem(
        [
            _job(
                "J",
                _op("p", "A", seconds=10),
                _op("s", "B", setup_detached=detached, **_after("p")),
            )
        ],
        [_machine("A"), _machine("B", setup_function_id=SETUP_3.id)],
        setup_functions=(SETUP_3,),
    )
    s = _a("s", "B", 7, 10, 15) if detached else _a("s", "B", 10, 13, 18)
    return Case(_compile(problem), _schedule(_a("p", "A", 0, 0, 10), s))


def _two_jobs() -> Case:
    problem = _problem([_job("J1", _op("a", "A")), _job("J2", _op("b", "A"))], [_machine("A")])
    return Case(_compile(problem), _schedule(_a("a", "A", 0, 0, 5), _a("b", "A", 5, 5, 10)))


def _calendar_forbid() -> Case:
    """A works [0, 10) and [20, 30); a 5 s forbid op fits in the first interval."""
    problem = _problem(
        [_job("J1", _op("a", "A"))],
        [_machine("A", calendar_id="cal")],
        calendars=(Calendar(id="cal", intervals=((0, 10), (20, 30))),),
    )
    return Case(_compile(problem), _schedule(_a("a", "A", 0, 0, 5)))


def _calendar_pause() -> Case:
    """A works [0, 5) and [10, 20); setup 2 from 3 ends at 5, 6 s of run resumes at 10."""
    problem = _problem(
        [_job("J1", _op("a", "A", seconds=6, shift_crossing="pause"))],
        [_machine("A", calendar_id="cal", setup_function_id=SETUP_2.id)],
        calendars=(Calendar(id="cal", intervals=((0, 5), (10, 20))),),
        setup_functions=(SETUP_2,),
    )
    return Case(_compile(problem), _schedule(_a("a", "A", 3, 5, 16)))


def _operators() -> Case:
    """Pool P (size 1, works [0, 20) and [30, 60)) used per phase by a, b and c.

    a needs P for its setup only, so b may hold P during a's run.
    """
    problem = _problem(
        [
            _job("J1", _op("a", "A", seconds=7, operator_needs=[_need("P", "setup")])),
            _job("J2", _op("b", "B", operator_needs=[_need("P", "run")])),
            _job("J3", _op("c", "B", seconds=2, operator_needs=[_need("P", "both")])),
        ],
        [_machine("A", setup_function_id=SETUP_3.id), _machine("B")],
        setup_functions=(SETUP_3,),
        operator_pools=(OperatorPool(id="P", size=1, calendar_id="shift"),),
        calendars=(Calendar(id="shift", intervals=((0, 20), (30, 60))),),
    )
    schedule = _schedule(_a("a", "A", 0, 3, 10), _a("b", "B", 3, 3, 8), _a("c", "B", 8, 8, 10))
    return Case(_compile(problem), schedule)


def _need(pool_id: str, phase: str) -> dict[str, Any]:
    return {"pool_id": pool_id, "quantity": 1, "phase": phase}


def _pause_pool() -> Case:
    """Machine always works; pool Q works [0, 5) and [10, 20): 8 s of pause work ends at 13."""
    problem = _problem(
        [
            _job(
                "J1",
                _op(
                    "d", "C", seconds=8, shift_crossing="pause", operator_needs=[_need("Q", "both")]
                ),
            )
        ],
        [_machine("C")],
        operator_pools=(OperatorPool(id="Q", size=1, calendar_id="q-shift"),),
        calendars=(Calendar(id="q-shift", intervals=((0, 5), (10, 20))),),
    )
    return Case(_compile(problem), _schedule(_a("d", "C", 0, 0, 13)))


def _batch_op(op_id: str, quantity: float, family: str) -> Operation:
    return Operation(
        id=op_id,
        work_center_id=WC,
        quantity=quantity,
        options=(_option("O", fixed=20.0, unit=0.0),),
        batch_family=family,
    )


def _batch() -> Case:
    """Oven O holds 10 kg. Batch b1 = a1 (4) + a2 (5) of family F; a3 (3, F) and a4 (1, G)."""
    problem = _problem(
        [
            _job("J1", _batch_op("a1", 4, "F")),
            _job("J2", _batch_op("a2", 5, "F")),
            _job("J3", _batch_op("a3", 3, "F")),
            _job("J4", _batch_op("a4", 1, "G")),
        ],
        [_machine("O", batch=BatchCapacity(capacity=10, unit="kg"))],
    )
    schedule = _schedule(
        _a("a1", "O", 0, 0, 20, "b1"),
        _a("a2", "O", 0, 0, 20, "b1"),
        _a("a3", "O", 30, 30, 50, "b2"),
        _a("a4", "O", 60, 60, 80, "b3"),
    )
    return Case(_compile(problem), schedule)


def _lags(*, detached: bool) -> Case:
    """p on A [0, 10); s after p with min_lag 2, max_lag 6, so its gate lies in [12, 16]."""
    s_op = _op("s", "B", setup_detached=detached, **_after("p", min_lag=2, max_lag=6))
    problem = _problem(
        [_job("J", _op("p", "A", seconds=10), s_op)],
        [_machine("A"), _machine("B", setup_function_id=SETUP_3.id if detached else None)],
        setup_functions=(SETUP_3,),
    )
    s = _a("s", "B", 11, 14, 19) if detached else _a("s", "B", 14, 14, 19)
    return Case(_compile(problem), _schedule(_a("p", "A", 0, 0, 10), s))


def _overlap() -> Case:
    """Transfer lot 3 of p (fixed 2, unit 1, qty 10, run 12 on [0, 12)).

    s1 (unit 0.5, run 5): gate >= 0 + 2 + 3*1 = 5, end >= 12 + 3*0.5 = 13.5 -> [8.5, 13.5).
    s2 (unit 2, run 20):  gate >= 5, end >= 12 + 3*2 = 18 -> [5, 25).
    """

    def lot_op(op_id: str, machine_id: str, fixed: float, unit: float, **fields: Any) -> Operation:
        return Operation.model_validate(
            {
                "id": op_id,
                "work_center_id": WC,
                "quantity": 10.0,
                "options": [_option(machine_id, fixed=fixed, unit=unit)],
            }
            | fields
        )

    problem = _problem(
        [
            _job(
                "J",
                lot_op("p", "A", 2, 1),
                lot_op("s1", "B", 0, 0.5, **_after("p", overlap_quantity=3)),
                lot_op("s2", "C", 0, 2, **_after("p", overlap_quantity=3)),
            )
        ],
        [_machine("A"), _machine("B"), _machine("C")],
    )
    schedule = _schedule(
        _a("p", "A", 0, 0, 12), _a("s1", "B", 8.5, 8.5, 13.5), _a("s2", "C", 5, 5, 25)
    )
    return Case(_compile(problem), schedule)


def _pin() -> Case:
    """x may run on A or B but is pinned to A with a start in [5, 7]."""
    pin = {"machine_id": "A", "earliest_start": 5, "latest_start": 7}
    problem = _problem([_job("J", _op("x", "A", "B", pin=pin))], [_machine("A"), _machine("B")])
    return Case(_compile(problem), _schedule(_a("x", "A", 6, 6, 11)))


def _in_progress() -> Case:
    """now = 10. x is in progress on A with 4 s left (its option says 9 s); d is done.

    y waits for the done op d; z is a plain op that starts at now.
    """
    status = {"state": "in_progress", "machine_id": "A", "remaining_seconds": 4}
    problem = _problem(
        [
            _job("J1", _op("x", "A", "B", seconds=9, status=status)),
            _job("J2", _op("d", "A", status={"state": "done"}), _op("y", "C", **_after("d"))),
            _job("J3", _op("z", "D", seconds=3)),
        ],
        [_machine("A"), _machine("B"), _machine("C"), _machine("D")],
        now=10.0,
    )
    schedule = _schedule(
        _a("x", "A", 10, 10, 14), _a("y", "C", 10, 10, 15), _a("z", "D", 10, 10, 13)
    )
    return Case(_compile(problem), schedule)


def _readiness() -> Case:
    """J1 released at 20; b has ready_time 30; machine B is available from 8 (c detached)."""
    problem = _problem(
        [
            _job("J1", _op("a", "A"), release=20.0),
            _job("J2", _op("b", "A", ready_time=30.0)),
            _job("J3", _op("c", "B", setup_detached=True)),
        ],
        [_machine("A"), _machine("B", state=MachineState(available_from=8.0))],
    )
    schedule = _schedule(_a("a", "A", 20, 20, 25), _a("b", "A", 30, 30, 35), _a("c", "B", 8, 8, 13))
    return Case(_compile(problem), schedule)


def _two_job_problem(**second_job: Any) -> Problem:
    return _problem(
        [_job("J1", _op("a", "A")), _job("J2", _op("b", "A"), **second_job)], [_machine("A")]
    )


def _hold() -> Case:
    """A hard hold without `until` removes J2 from the solve; only a is scheduled."""
    hold = HoldOverride(id="h", hardness="hard", job_id="J2", **_meta())
    return Case(_compile(_two_job_problem(), [hold]), _schedule(_a("a", "A", 0, 0, 5)), (hold,))


def _hold_until() -> Case:
    """A hard hold until 30 moves J1's release to 30."""
    hold = HoldOverride(id="h", hardness="hard", job_id="J1", until=30.0, **_meta())
    schedule = _schedule(_a("a", "A", 30, 30, 35), _a("b", "A", 0, 0, 5))
    return Case(_compile(_two_job_problem(), [hold]), schedule, (hold,))


def _held_in_problem() -> Case:
    """J2 is listed in held_job_ids while still in the problem; it must not be scheduled."""
    compiled = CompiledProblem(
        problem=_two_job_problem(), profile=SchedulingProfile(), held_job_ids=("J2",)
    )
    return Case(compiled, _schedule(_a("a", "A", 0, 0, 5)))


def _held_status() -> Case:
    """J2 has input status held; it needs no assignment."""
    return Case(_compile(_two_job_problem(status="held")), _schedule(_a("a", "A", 0, 0, 5)))


def _split() -> Case:
    """J1's lot of 10 (1 s per unit) splits in halves: a~1 and a~2 run 5 s each."""
    op = Operation(
        id="a", work_center_id=WC, quantity=10.0, options=(_option("A", fixed=0.0, unit=1.0),)
    )
    split = SplitOverride(id="s", hardness="hard", job_id="J1", fractions=(0.5, 0.5), **_meta())
    compiled = _compile(_problem([_job("J1", op)], [_machine("A")]), [split])
    schedule = _schedule(_a("a~1", "A", 0, 0, 5), _a("a~2", "A", 5, 5, 10))
    return Case(compiled, schedule, (split,))


def _overrides_honored() -> Case:
    """Hard lock, hard exclude, hard prefer and a soft exclude, all honored."""
    overrides: tuple[Override, ...] = (
        LockOverride(
            id="o1", hardness="hard", operation_id="x", machine_id="A", start=5.0, **_meta()
        ),
        ExcludeMachineOverride(
            id="o2", hardness="hard", operation_id="y", machine_id="B", **_meta()
        ),
        PreferMachineOverride(
            id="o3", hardness="hard", operation_id="z", machine_id="A", **_meta()
        ),
        ExcludeMachineOverride(
            id="o4", hardness="soft", operation_id="w", machine_id="A", penalty=3.0, **_meta()
        ),
    )
    problem = _problem(
        [_job(f"J{op}", _op(op, "A", "B")) for op in ("x", "y", "z", "w")],
        [_machine("A"), _machine("B")],
    )
    schedule = _schedule(
        _a("x", "A", 5, 5, 10),
        _a("y", "A", 10, 10, 15),
        _a("z", "A", 15, 15, 20),
        _a("w", "A", 20, 20, 25),
    )
    return Case(_compile(problem, overrides), schedule, overrides)


CASES: dict[str, Callable[[], Case]] = {
    "pinedo_7_1_1": _pinedo_7_1_1,
    "pinedo_14_2_1": _pinedo_14_2_1,
    "setups_attached": functools.partial(_setups, detached=False),
    "setups_detached": functools.partial(_setups, detached=True),
    "two_jobs": _two_jobs,
    "calendar_forbid": _calendar_forbid,
    "calendar_pause": _calendar_pause,
    "operators": _operators,
    "pause_pool": _pause_pool,
    "batch": _batch,
    "lags_attached": functools.partial(_lags, detached=False),
    "lags_detached": functools.partial(_lags, detached=True),
    "overlap": _overlap,
    "pin": _pin,
    "in_progress": _in_progress,
    "readiness": _readiness,
    "hold": _hold,
    "hold_until": _hold_until,
    "held_in_problem": _held_in_problem,
    "held_status": _held_status,
    "split": _split,
    "overrides_honored": _overrides_honored,
}


# --- unmutated base cases verify -------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CASES))
def test_verify_base_case_is_valid(name: str) -> None:
    case = CASES[name]()

    result = _check(case)

    assert result.valid, result.issues
    assert result.issues == ()


@pytest.mark.parametrize(
    ("name", "kind", "optimum"),
    [
        ("pinedo_7_1_1.json", ObjectiveKind.MAKESPAN, 28.0),
        ("pinedo_14_2_1.json", ObjectiveKind.WEIGHTED_TARDINESS, 98.0),
    ],
)
def test_verify_oracle_schedule_with_its_primary_value_is_valid(
    name: str, kind: ObjectiveKind, optimum: float
) -> None:
    case = _fixture_case(name, kind)
    assert case.schedule.primary_value == optimum

    assert _check(case).valid


def test_verify_uses_start_order_not_list_order_for_setups() -> None:
    case = _pinedo_14_2_1()

    result = _check(case, reversed_order(case.schedule))

    assert result.valid, result.issues


def test_verify_is_deterministic() -> None:
    case = _pinedo_14_2_1()
    mutated = shift(shorten_setup(case.schedule, "1-1", 1), "4-1", -1)

    assert _check(case, mutated) == _check(case, mutated)


# --- zero-length operations on a machine (TF-002 review) -------------------------------------


def _empty_beside(long_id: str, empty_id: str) -> Case:
    """`long_id` runs 10 s on A from 0; `empty_id` has a 0 s run on A."""
    problem = _problem(
        [_job("J1", _op(long_id, "A", seconds=10)), _job("J2", _op(empty_id, "A", seconds=0))],
        [_machine("A")],
    )
    return Case(_compile(problem), _schedule())


@pytest.mark.parametrize(("long_id", "empty_id"), [("a", "z"), ("z", "a")])
def test_verify_empty_span_at_an_ops_start_is_valid_whatever_the_ids(
    long_id: str, empty_id: str
) -> None:
    case = _empty_beside(long_id, empty_id)
    schedule = _schedule(_a(long_id, "A", 0, 0, 10), _a(empty_id, "A", 0, 0, 0))

    assert _check(case, schedule).valid


@pytest.mark.parametrize(("long_id", "empty_id"), [("a", "z"), ("z", "a")])
def test_verify_empty_span_at_an_ops_end_is_valid_whatever_the_ids(
    long_id: str, empty_id: str
) -> None:
    case = _empty_beside(long_id, empty_id)
    schedule = _schedule(_a(long_id, "A", 0, 0, 10), _a(empty_id, "A", 10, 10, 10))

    assert _check(case, schedule).valid


def test_verify_empty_span_strictly_inside_an_op_is_machine_overlap() -> None:
    case = _empty_beside("a", "z")
    schedule = _schedule(_a("a", "A", 0, 0, 10), _a("z", "A", 5, 5, 5))

    _only(_check(case, schedule), "machine_overlap")


# --- pause starts on a working instant (TF-002 review) ---------------------------------------


def _pause_after(seconds: float) -> Case:
    """a on A [0, 10); b (pause, `seconds` s) on B working [0, 10) and [20, 40), max_lag 0."""
    problem = _problem(
        [
            _job(
                "J",
                _op("a", "A", seconds=10),
                _op("b", "B", seconds=seconds, shift_crossing="pause", **_after("a", max_lag=0)),
            )
        ],
        [_machine("A"), _machine("B", calendar_id="cal")],
        calendars=(Calendar(id="cal", intervals=((0, 10), (20, 40))),),
    )
    return Case(_compile(problem), _schedule())


def test_verify_pause_op_starting_inside_a_break_is_calendar() -> None:
    case = _pause_after(5)
    schedule = _schedule(_a("a", "A", 0, 0, 10), _a("b", "B", 10, 10, 25))

    issues = _only(_check(case, schedule), "calendar")
    assert _names(issues, "b")


def test_verify_zero_length_pause_op_inside_a_break_is_calendar() -> None:
    case = _pause_after(0)
    schedule = _schedule(_a("a", "A", 0, 0, 10), _a("b", "B", 10, 10, 10))

    _only(_check(case, schedule), "calendar")


def test_verify_pause_op_starting_where_work_begins_is_valid() -> None:
    case = _pause_after(5)
    schedule = _schedule(_a("a", "A", 10, 10, 20), _a("b", "B", 20, 20, 25))

    assert _check(case, schedule).valid


# --- held predecessors (TF-002 review) -------------------------------------------------------


def test_verify_op_scheduled_after_an_op_of_a_held_job_is_precedence() -> None:
    problem = _problem(
        [_job("J", _op("a", "A"), status="held"), _job("K", _op("b", "A", **_after("a")))],
        [_machine("A")],
    )
    case = Case(_compile(problem), _schedule())

    issues = _only(_check(case, _schedule(_a("b", "A", 0, 0, 5))), "precedence")
    assert _names(issues, "a")


# --- one mutation, exactly one family ---------------------------------------------------------

MUTATIONS = [
    # coverage
    pytest.param(
        "pinedo_7_1_1", lambda s: drop(s, "1-3"), "missing_assignment", "1-3", id="missing"
    ),
    pytest.param(
        "split", lambda s: drop(s, "a~2"), "missing_assignment", "a~2", id="missing-split-child"
    ),
    pytest.param(
        "pinedo_7_1_1",
        lambda s: add(s, _a("ghost", "M1", 1000, 1000, 1001)),
        "unknown_assignment",
        "ghost",
        id="unknown",
    ),
    pytest.param(
        "held_in_problem",
        lambda s: add(s, _a("b", "A", 100, 100, 105)),
        "held_scheduled",
        "b",
        id="held-by-compile",
    ),
    pytest.param(
        "held_status",
        lambda s: add(s, _a("b", "A", 100, 100, 105)),
        "held_scheduled",
        "b",
        id="held-by-status",
    ),
    # finiteness
    pytest.param(
        "pinedo_7_1_1", lambda s: with_nonfinite(s, "1-3", "end"), "nonfinite", "1-3", id="nan-end"
    ),
    pytest.param(
        "pinedo_7_1_1",
        lambda s: with_nonfinite(s, "1-3", "start", -math.inf),
        "nonfinite",
        "1-3",
        id="inf-start",
    ),
    pytest.param(
        "pinedo_7_1_1", lambda s: with_primary(s, math.inf), "nonfinite", None, id="inf-primary"
    ),
    # eligibility and lengths
    pytest.param(
        "pinedo_7_1_1",
        lambda s: replace(s, "1-3", machine_id="M1", start=1000, setup_end=1000, end=1004),
        "ineligible_machine",
        "1-3",
        id="ineligible",
    ),
    pytest.param(
        "pinedo_7_1_1",
        lambda s: move_to_machine(s, "1-3", "M9"),
        "ineligible_machine",
        "1-3",
        id="unknown-machine",
    ),
    pytest.param(
        "pinedo_7_1_1", lambda s: shorten_run(s, "1-3", 1), "duration", "1-3", id="duration-short"
    ),
    pytest.param(
        "two_jobs", lambda s: shorten_run(s, "b", -1), "duration", "b", id="duration-long"
    ),
    pytest.param(
        "pinedo_14_2_1", lambda s: shorten_setup(s, "1-1", 1), "setup", "1-1", id="setup-short"
    ),
    pytest.param(
        "setups_attached",
        lambda s: shorten_setup(s, "s", 1),
        "setup",
        "s",
        id="setup-short-attached",
    ),
    # machines and calendars
    pytest.param(
        "two_jobs", lambda s: shift(s, "b", -2), "machine_overlap", "b", id="overlap-hand"
    ),
    pytest.param(
        "pinedo_14_2_1",
        lambda s: shift(s, "4-1", -1),
        "machine_overlap",
        "4-1",
        id="overlap-setups",
    ),
    pytest.param(
        "calendar_forbid", lambda s: shift(s, "a", 7), "calendar", "a", id="forbid-crosses-break"
    ),
    pytest.param(
        "calendar_forbid", lambda s: shift(s, "a", 12), "calendar", "a", id="forbid-inside-break"
    ),
    pytest.param(
        "calendar_pause",
        lambda s: shorten_run(s, "a", 5),
        "calendar",
        "a",
        id="pause-ignores-break",
    ),
    # readiness and precedence
    pytest.param("readiness", lambda s: shift(s, "a", -1), "readiness", "a", id="before-release"),
    pytest.param(
        "readiness", lambda s: shift(s, "b", -1), "readiness", "b", id="before-ready-time"
    ),
    pytest.param(
        "readiness", lambda s: shift(s, "c", -1), "readiness", "c", id="before-available-from"
    ),
    pytest.param("in_progress", lambda s: shift(s, "z", -1), "readiness", "z", id="before-now"),
    pytest.param("lags_attached", lambda s: shift(s, "s", -3), "precedence", "s", id="min-lag"),
    pytest.param(
        "lags_detached", lambda s: shift(s, "s", -3), "precedence", "s", id="min-lag-detached"
    ),
    pytest.param("lags_attached", lambda s: shift(s, "s", 3), "max_lag", "s", id="max-lag"),
    pytest.param(
        "lags_detached", lambda s: shift(s, "s", 3), "max_lag", "s", id="max-lag-detached"
    ),
    pytest.param(
        "overlap", lambda s: shift(s, "s1", -1.5), "overlap_lag", "s1", id="overlap-end-rule"
    ),
    pytest.param(
        "overlap", lambda s: shift(s, "s2", -1), "overlap_lag", "s2", id="overlap-gate-rule"
    ),
    # operators
    pytest.param(
        "operators", lambda s: shift(s, "b", -1), "operator_capacity", "b", id="operator-capacity"
    ),
    pytest.param(
        "operators", lambda s: shift(s, "c", 11), "operator_calendar", "c", id="operator-calendar"
    ),
    # batches
    pytest.param("batch", lambda s: shift(s, "a2", 1), "batch", "a2", id="batch-not-aligned"),
    pytest.param(
        "batch", lambda s: join_batch(s, "a3", "a1"), "batch", "a3", id="batch-over-capacity"
    ),
    pytest.param(
        "batch", lambda s: join_batch(s, "a4", "a1"), "batch", "a4", id="batch-mixed-family"
    ),
    # pins and shop-floor status
    pytest.param("pin", lambda s: shift(s, "x", -2), "pin", "x", id="pin-too-early"),
    pytest.param("pin", lambda s: shift(s, "x", 2), "pin", "x", id="pin-too-late"),
    pytest.param("pin", lambda s: move_to_machine(s, "x", "B"), "pin", "x", id="pin-machine"),
    pytest.param(
        "in_progress", lambda s: shorten_run(s, "x", 1), "in_progress", "x", id="in-progress-length"
    ),
    pytest.param(
        "in_progress", lambda s: shift(s, "x", 1), "in_progress", "x", id="in-progress-start"
    ),
    pytest.param(
        "in_progress",
        lambda s: move_to_machine(s, "x", "B"),
        "in_progress",
        "x",
        id="in-progress-machine",
    ),
    # objective
    pytest.param(
        "pinedo_7_1_1", lambda s: with_primary(s, 27.0), "objective_mismatch", None, id="objective"
    ),
    pytest.param(
        "pinedo_7_1_1",
        lambda s: with_primary(s, 28.0 + 1e-4),
        "objective_mismatch",
        None,
        id="objective-rel",
    ),
]


@pytest.mark.parametrize(("name", "mutate", "code", "operation_id"), MUTATIONS)
def test_verify_single_mutation_yields_exactly_its_family_code(
    name: str, mutate: Mutation, code: str, operation_id: str | None
) -> None:
    case = CASES[name]()

    issues = _only(_check(case, mutate(case.schedule)), code)

    if operation_id is not None:
        assert _names(issues, operation_id), issues


# --- material gate: attached vs detached setup (PLAN T-097 cases) -----------------------------


def test_verify_attached_setup_starting_before_predecessor_end_is_precedence_at_start() -> None:
    attached = _setups(detached=False)
    detached_schedule = _setups(detached=True).schedule  # s setup on [7, 10), p ends at 10

    issues = _only(_check(attached, detached_schedule), "precedence")

    assert all("setup_end" not in i.message and "start" in i.message for i in issues), issues


def test_verify_same_schedule_with_detached_setup_is_valid() -> None:
    detached = _setups(detached=True)

    assert detached.schedule.assignments[1].start < detached.schedule.assignments[0].end
    assert _check(detached).valid


def test_verify_detached_run_before_predecessor_end_is_precedence_at_setup_end() -> None:
    detached = _setups(detached=True)

    issues = _only(_check(detached, shift(detached.schedule, "s", -1)), "precedence")

    assert all("setup_end" in i.message for i in issues), issues


def _detached_with_ready_time() -> Case:
    """s is detached with ready_time 12; p (its predecessor) ends at 10."""
    problem = _problem(
        [
            _job(
                "J",
                _op("p", "A", seconds=10),
                _op("s", "B", setup_detached=True, ready_time=12.0, **_after("p")),
            )
        ],
        [_machine("A"), _machine("B", setup_function_id=SETUP_3.id)],
        setup_functions=(SETUP_3,),
    )
    return Case(_compile(problem), _schedule(_a("p", "A", 0, 0, 10), _a("s", "B", 9, 12, 17)))


def test_verify_detached_setup_may_start_before_ready_time() -> None:
    assert _check(_detached_with_ready_time()).valid


def test_verify_detached_run_before_ready_time_is_readiness_at_setup_end() -> None:
    case = _detached_with_ready_time()
    early = replace(case.schedule, "s", start=7, setup_end=10, end=15)

    issues = _only(_check(case, early), "readiness")

    assert all("setup_end" in i.message for i in issues), issues


@pytest.mark.parametrize("operation_id", ["a", "b", "c"])
def test_verify_readiness_on_start_gate_names_start(operation_id: str) -> None:
    case = _readiness()

    issues = _only(_check(case, shift(case.schedule, operation_id, -1)), "readiness")

    assert all("setup_end" not in i.message and "start" in i.message for i in issues), issues


# --- hard overrides checked against the raw override list ------------------------------------
#
# The compiled problem here deliberately does NOT contain the overrides (as if compilation
# had dropped them), so only the raw-override check can see the violation and no pin or
# eligibility issue is raised alongside it.


def _uncompiled_case(*overrides: Override) -> Case:
    problem = _problem([_job("J", _op("x", "A", "B"))], [_machine("A"), _machine("B")])
    return Case(_compile(problem), _schedule(_a("x", "A", 0, 0, 5)), tuple(overrides))


VIOLATED_OVERRIDES = [
    pytest.param(
        LockOverride(id="o", hardness="hard", operation_id="x", machine_id="B", **_meta()),
        id="lock-machine",
    ),
    pytest.param(
        LockOverride(id="o", hardness="hard", operation_id="x", start=3.0, **_meta()),
        id="lock-start",
    ),
    pytest.param(
        ExcludeMachineOverride(
            id="o", hardness="hard", operation_id="x", machine_id="A", **_meta()
        ),
        id="exclude-op",
    ),
    pytest.param(
        ExcludeMachineOverride(id="o", hardness="hard", job_id="J", machine_id="A", **_meta()),
        id="exclude-job",
    ),
    pytest.param(
        PreferMachineOverride(id="o", hardness="hard", operation_id="x", machine_id="B", **_meta()),
        id="prefer-op",
    ),
    pytest.param(
        HoldOverride(id="o", hardness="hard", job_id="J", until=2.0, **_meta()),
        id="hold-until",
    ),
]


@pytest.mark.parametrize("override", VIOLATED_OVERRIDES)
def test_verify_hard_override_not_honored_is_override(override: Override) -> None:
    case = _uncompiled_case(override)

    issues = _only(_check(case), "override")

    assert any(override.id in f"{i.path} {i.message}" for i in issues), issues


def test_verify_hold_without_until_on_scheduled_job_is_held_scheduled() -> None:
    hold = HoldOverride(id="o", hardness="hard", job_id="J", **_meta())

    _only(_check(_uncompiled_case(hold)), "held_scheduled")


@pytest.mark.parametrize(
    "override",
    [
        pytest.param(
            ExcludeMachineOverride(
                id="o", hardness="soft", operation_id="x", machine_id="A", penalty=5.0, **_meta()
            ),
            id="soft-exclude",
        ),
        pytest.param(
            PreferMachineOverride(
                id="o", hardness="soft", operation_id="x", machine_id="B", penalty=5.0, **_meta()
            ),
            id="soft-prefer",
        ),
        pytest.param(
            LockOverride(
                id="o", hardness="hard", operation_id="x", machine_id="A", start=0.0, **_meta()
            ),
            id="lock-honored",
        ),
    ],
)
def test_verify_soft_or_honored_override_is_valid(override: Override) -> None:
    assert _check(_uncompiled_case(override)).valid


# --- hard overrides on a split job resolve to its children (TF-002 review) --------------------


def _split_with(override: Override) -> Case:
    """Job J (op a, 5 s on A or B) split in halves; `override` on J or a is raw only.

    The compiled problem holds the split alone, as if the compile step lost the override,
    so only the verifier's own resolution through `split_map` can enforce it (D-066).
    """
    problem = _problem([_job("J", _op("a", "A", "B"))], [_machine("A"), _machine("B")])
    split = SplitOverride(id="split", hardness="hard", job_id="J", fractions=(0.5, 0.5), **_meta())
    return Case(_compile(problem, (split,)), _schedule(), (split, override))


_ON_SPLIT = {
    "lock": LockOverride(id="o", hardness="hard", operation_id="a", machine_id="A", **_meta()),
    "exclude_job": ExcludeMachineOverride(
        id="o", hardness="hard", job_id="J", machine_id="B", **_meta()
    ),
    "exclude_op": ExcludeMachineOverride(
        id="o", hardness="hard", operation_id="a", machine_id="B", **_meta()
    ),
    "prefer_job": PreferMachineOverride(
        id="o", hardness="hard", job_id="J", machine_id="A", **_meta()
    ),
}


@pytest.mark.parametrize("name", sorted(_ON_SPLIT))
def test_verify_hard_machine_override_on_a_split_target_holds_on_every_child(name: str) -> None:
    case = _split_with(_ON_SPLIT[name])
    honored = _schedule(_a("a~1", "A", 0, 0, 5), _a("a~2", "A", 5, 5, 10))
    broken = _schedule(_a("a~1", "A", 0, 0, 5), _a("a~2", "B", 0, 0, 5))

    assert _check(case, honored).valid
    issues = _only(_check(case, broken), "override")
    assert issues[0].path == "o"


def test_verify_hold_until_on_a_split_job_holds_on_every_child() -> None:
    hold = HoldOverride(id="o", hardness="hard", job_id="J", until=3.0, **_meta())
    case = _split_with(hold)
    honored = _schedule(_a("a~1", "A", 3, 3, 8), _a("a~2", "B", 3, 3, 8))
    broken = _schedule(_a("a~1", "A", 3, 3, 8), _a("a~2", "B", 1, 1, 6))

    assert _check(case, honored).valid
    _only(_check(case, broken), "override")


# --- frozen zone, checked against the previous plan (TF-002 review) ---------------------------


FROZEN = FrozenZone(until=10.0, tolerance=1.0)
PREVIOUS = PreviousPlan(
    plan_id="prev",
    problem_digest="d",
    assignments=(
        PreviousAssignment(operation_id="a", machine_id="A", start=4.0),
        PreviousAssignment(operation_id="b", machine_id="B", start=30.0),
    ),
)


def _frozen_case(*raw: Override, **job_fields: Any) -> Case:
    """a (5 s on A or B) and b (5 s on B) with a frozen zone whose pins were never compiled.

    The compiled problem carries the profile's frozen zone but no frozen pin, as if the
    compile step had skipped it: only the verifier's own check can catch a moved op.
    """
    problem = _problem(
        [_job("J", _op("a", "A", "B"), **job_fields), _job("K", _op("b", "B"))],
        [_machine("A"), _machine("B")],
    )
    compiled = _compile(problem).model_copy(
        update={"profile": SchedulingProfile(frozen_zone=FROZEN)}
    )
    return Case(compiled, _schedule(), raw)


def _frozen_check(case: Case, schedule: Schedule) -> VerificationResult:
    result = verifier.verify(case.compiled, case.raw_overrides, schedule, PREVIOUS)
    assert result.valid is (not result.issues), result
    return result


@pytest.mark.parametrize(
    "a",
    [_a("a", "A", 4, 4, 9), _a("a", "A", 3, 3, 8), _a("a", "A", 5, 5, 10)],
)
def test_verify_frozen_op_on_its_previous_machine_within_tolerance_is_valid(
    a: Assignment,
) -> None:
    schedule = _schedule(a, _a("b", "B", 0, 0, 5))

    assert _frozen_check(_frozen_case(), schedule).valid


@pytest.mark.parametrize(
    "a",
    [_a("a", "A", 0, 0, 5), _a("a", "A", 6, 6, 11), _a("a", "B", 4, 4, 9)],
)
def test_verify_frozen_op_moved_or_on_another_machine_is_frozen(a: Assignment) -> None:
    schedule = _schedule(a, _a("b", "B", 10, 10, 15))

    issues = _only(_frozen_check(_frozen_case(), schedule), "frozen")
    assert [issue.path for issue in issues] == ["a"]


def test_verify_without_previous_plan_skips_the_frozen_check() -> None:
    schedule = _schedule(_a("a", "A", 0, 0, 5), _a("b", "B", 0, 0, 5))

    assert _check(_frozen_case(), schedule).valid


_RELEASING = {
    "lock": LockOverride(id="o", hardness="hard", operation_id="a", start=0.0, **_meta()),
    "exclude_previous": ExcludeMachineOverride(
        id="o", hardness="hard", job_id="J", machine_id="A", **_meta()
    ),
    "prefer_other": PreferMachineOverride(
        id="o", hardness="hard", operation_id="a", machine_id="B", **_meta()
    ),
}


@pytest.mark.parametrize("name", sorted(_RELEASING))
def test_verify_frozen_op_released_by_a_raw_hard_override_is_not_frozen(name: str) -> None:
    case = _frozen_case(_RELEASING[name])
    schedule = _schedule(_a("a", "B", 0, 0, 5), _a("b", "B", 5, 5, 10))

    assert "frozen" not in _codes(_frozen_check(case, schedule))


def test_verify_soft_override_does_not_release_a_frozen_op() -> None:
    soft = PreferMachineOverride(
        id="o", hardness="soft", operation_id="a", machine_id="B", penalty=1.0, **_meta()
    )
    schedule = _schedule(_a("a", "B", 0, 0, 5), _a("b", "B", 5, 5, 10))

    _only(_frozen_check(_frozen_case(soft), schedule), "frozen")


def test_verify_frozen_op_of_a_held_job_is_not_checked() -> None:
    case = _frozen_case(status="held")

    assert _frozen_check(case, _schedule(_a("b", "B", 0, 0, 5))).valid


# --- objective -------------------------------------------------------------------------------


def test_verify_primary_value_within_relative_tolerance_is_valid() -> None:
    case = _pinedo_7_1_1()

    result = _check(case, with_primary(case.schedule, 28.0 + 2e-5))  # rel 7e-7 < 1e-6

    assert result.valid, result.issues


def test_verify_primary_value_is_read_from_the_compiled_profile_objective() -> None:
    weighted = _fixture_case("pinedo_14_2_1.json", ObjectiveKind.WEIGHTED_TARDINESS)
    default_profile = weighted.compiled.model_copy(update={"profile": SchedulingProfile()})

    result = verifier.verify(default_profile, (), weighted.schedule)  # 98 is not the makespan (48)

    assert _codes(result) == {"objective_mismatch"}


def test_verify_without_primary_value_skips_the_objective_check() -> None:
    case = _pinedo_7_1_1()

    assert case.schedule.primary_value is None
    assert _check(case).valid


# --- module boundary -------------------------------------------------------------------------


SCHEDULING = "twinflow.scheduling"


def _twinflow_imports(tree: ast.Module) -> set[str]:
    """Modules imported from twinflow; `from twinflow.scheduling import x` counts as `.x`."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module == SCHEDULING:
            found |= {f"{SCHEDULING}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return {name for name in found if name.startswith("twinflow")}


def test_verifier_imports_only_model_setups_objectives_and_calendar_math() -> None:
    allowed = {
        f"{SCHEDULING}.{name}" for name in ("model", "setups", "objectives", "calendar_math")
    }

    imported = _twinflow_imports(ast.parse(Path(verifier.__file__).read_text()))

    assert imported <= allowed, imported - allowed
