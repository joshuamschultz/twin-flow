"""CP-SAT adapter tests (T-103, T-134, T-135; COMP-010, COMP-016; REQ-076, REQ-078,
REQ-104, REQ-106; D-067, D-068, D-075).

Every plan goes through `solver.solve`, so the adapter's answers are checked by the
independent verifier and evaluated, not trusted. Optimal values are compared with the
brute-force oracle (`oracle.py`, `oracle_math.py`), which shares no arithmetic with the
production code. The module is skipped when the optional `ortools` extra is absent.
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("ortools")

from oracle import (  # noqa: E402
    SequenceDeadlock,
    all_sequences,
    brute_force,
    combination_count,
    semi_active,
)
from oracle_math import objective_values  # noqa: E402

from twinflow.scheduling import get_solver  # noqa: E402
from twinflow.scheduling.algorithms.cpsat import ORToolsSolver  # noqa: E402
from twinflow.scheduling.model import (  # noqa: E402
    Assignment,
    CompiledProblem,
    EpsilonBound,
    EpsilonConstraint,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    ObjectiveSpec,
    Operation,
    PreviousPlan,
    Problem,
    SchedulingProfile,
    SolveRequest,
    SolveResult,
    WeightedSum,
    WeightedTerm,
    WorkCenter,
)
from twinflow.scheduling.seam import Feature, OptionalDependencyError, SolveContext  # noqa: E402
from twinflow.scheduling.solver import solve  # noqa: E402
from twinflow.scheduling.verifier import verify  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
WC = "WC"


# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, runs: dict[str, float] | None = None, **fields: Any) -> Operation:
    """An operation with `runs` fixed seconds per machine (default: 5 s on M1)."""
    options = [
        {"machine_id": m, "fixed_seconds": s, "unit_seconds": 0.0, "preference_cost": 0.0}
        for m, s in (runs or {"M1": 5.0}).items()
    ]
    data = {"id": op_id, "work_center_id": WC, "quantity": 1.0, "options": options}
    return Operation.model_validate(data | fields)


def _machine(machine_id: str = "M1", **fields: Any) -> Machine:
    return Machine.model_validate({"id": machine_id, "work_center_id": WC} | fields)


def _problem(
    jobs: Sequence[Job | Sequence[Operation]],
    machines: Sequence[Machine] = (),
    **fields: Any,
) -> Problem:
    """Jobs J0, J1, ... (a plain operation list becomes a job); machines default to M1, M2."""
    built = tuple(
        job if isinstance(job, Job) else Job(id=f"J{k}", operations=tuple(job))
        for k, job in enumerate(jobs)
    )
    data: dict[str, Any] = {
        "id": "p",
        "jobs": built,
        "machines": tuple(machines) or (_machine("M1"), _machine("M2")),
        "work_centers": (WorkCenter(id=WC),),
    }
    return Problem.model_validate(data | fields)


def _request(problem: Problem, kind: ObjectiveKind = ObjectiveKind.MAKESPAN) -> SolveRequest:
    profile = SchedulingProfile(objective=Lexicographic(levels=(LexicographicLevel(kind=kind),)))
    return SolveRequest(schema_version="2.0", problem=problem, profile=profile)


def _solve(problem: Problem, kind: ObjectiveKind = ObjectiveKind.MAKESPAN) -> SolveResult:
    return solve(_request(problem, kind), solver=ORToolsSolver(), budget_seconds=10.0)


def _placed(result: SolveResult) -> dict[str, Assignment]:
    return {a.operation_id: a for a in result.schedule.assignments}


def _codes(result: SolveResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _assert_optimal_and_verified(result: SolveResult) -> None:
    assert result.schedule.status == "OPTIMAL", result.issues
    assert result.verified, result.issues
    assert all(a.setup_end == a.start for a in result.schedule.assignments)


# --- identity ------------------------------------------------------------------------------


def test_name_and_capabilities_are_exactly_the_sdd_set() -> None:
    """T-135 adds setups and detached setups; T-136/T-137 add overlap, restart, batching."""
    solver = ORToolsSolver()
    assert solver.name == "ortools"
    assert solver.capabilities == frozenset(
        {
            Feature.ALTERNATIVES,
            Feature.CALENDARS,
            Feature.OPERATORS,
            Feature.LAGS,
            Feature.MAX_LAGS,
            Feature.PINS,
            Feature.IN_PROGRESS,
            Feature.MACHINE_STATE,
            Feature.SETUPS,
            Feature.DETACHED_SETUP,
        }
    )


def test_constructor_rejects_bad_workers_and_seed() -> None:
    with pytest.raises(ValueError):
        ORToolsSolver(workers=0)
    with pytest.raises(ValueError):
        ORToolsSolver(seed=-1)


def test_missing_ortools_raises_optional_dependency_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ortools.sat.python.cp_model", None)
    with pytest.raises(OptionalDependencyError):
        ORToolsSolver()


# --- Pinedo 7.1.1 ----------------------------------------------------------------------------


def test_pinedo_7_1_1_is_optimal_at_28_and_matches_the_oracle() -> None:
    request = SolveRequest.model_validate_json((FIXTURES / "pinedo_7_1_1.json").read_text())
    oracle_value, _ = brute_force(request.problem, ObjectiveKind.MAKESPAN)

    result = solve(request, solver=ORToolsSolver(), budget_seconds=10.0)

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(28.0)
    assert result.objective_values[ObjectiveKind.MAKESPAN] == pytest.approx(28.0)
    assert oracle_value == pytest.approx(28.0)
    assert result.schedule.best_bound == pytest.approx(28.0)


def test_same_input_gives_the_same_plan() -> None:
    request = SolveRequest.model_validate_json((FIXTURES / "pinedo_7_1_1.json").read_text())
    first = solve(request, solver=ORToolsSolver(), budget_seconds=10.0)
    second = solve(request, solver=ORToolsSolver(), budget_seconds=10.0)
    assert first.schedule.assignments == second.schedule.assignments


# --- supported features --------------------------------------------------------------------


def test_alternatives_pick_the_faster_machine() -> None:
    result = _solve(_problem([[_op("A", {"M1": 10.0, "M2": 4.0})]]))

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].machine_id == "M2"
    assert result.schedule.primary_value == pytest.approx(4.0)


def test_alternatives_spread_two_ops_over_both_machines() -> None:
    # M2 is slower but not twice as slow: spreading (makespan 5) beats both on M1 (6).
    ops = [_op("A", {"M1": 3.0, "M2": 5.0}), _op("B", {"M1": 3.0, "M2": 5.0})]
    result = _solve(_problem([ops]))

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(5.0)
    assert {a.machine_id for a in result.schedule.assignments} == {"M1", "M2"}


def test_machine_calendar_forbids_crossing_a_break() -> None:
    calendar = {"id": "c", "intervals": [(0.0, 5.0), (10.0, 30.0)]}
    problem = _problem(
        [[_op("A", {"M1": 6.0})]], (_machine("M1", calendar_id="c"),), calendars=(calendar,)
    )

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].start == pytest.approx(10.0)


def test_touching_calendar_intervals_form_one_working_run() -> None:
    calendar = {"id": "c", "intervals": [(0.0, 3.0), (3.0, 30.0)]}
    problem = _problem(
        [[_op("A", {"M1": 6.0})]], (_machine("M1", calendar_id="c"),), calendars=(calendar,)
    )

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].start == pytest.approx(0.0)


def test_operator_pool_capacity_serializes_ops_on_different_machines() -> None:
    need = [{"pool_id": "P", "quantity": 1, "phase": "both"}]
    ops = [_op("A", {"M1": 5.0}, operator_needs=need), _op("B", {"M2": 5.0}, operator_needs=need)]
    problem = _problem([ops], operator_pools=({"id": "P", "size": 1},))

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(10.0)


def test_operator_quantity_counts_against_pool_size() -> None:
    need_two = [{"pool_id": "P", "quantity": 2, "phase": "run"}]
    need_one = [{"pool_id": "P", "quantity": 1, "phase": "run"}]
    ops = [
        _op("A", {"M1": 5.0}, operator_needs=need_two),
        _op("B", {"M2": 5.0}, operator_needs=need_one),
    ]
    problem = _problem([ops], operator_pools=({"id": "P", "size": 3},))

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(5.0)


def test_pool_calendar_forbids_crossing_an_operator_break() -> None:
    calendar = {"id": "shift", "intervals": [(0.0, 3.0), (7.0, 20.0)]}
    need = [{"pool_id": "P", "quantity": 1, "phase": "setup"}]
    problem = _problem(
        [[_op("A", {"M1": 4.0}, operator_needs=need)]],
        calendars=(calendar,),
        operator_pools=({"id": "P", "size": 1, "calendar_id": "shift"},),
    )

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].start == pytest.approx(7.0)


def test_min_lag_delays_the_successor() -> None:
    first = _op("A", {"M1": 5.0})
    second = _op("B", {"M2": 5.0}, predecessors=[{"operation_id": "A", "min_lag": 2.0}])

    result = _solve(_problem([[first, second]]))

    _assert_optimal_and_verified(result)
    assert _placed(result)["B"].start == pytest.approx(7.0)


def test_max_lag_delays_the_predecessor() -> None:
    first = _op("A", {"M1": 5.0})
    second = _op("B", {"M2": 5.0}, predecessors=[{"operation_id": "A", "max_lag": 0.0}])
    machines = (_machine("M1"), _machine("M2", state={"available_from": 10.0}))

    result = _solve(_problem([[first, second]], machines))

    _assert_optimal_and_verified(result)
    placed = _placed(result)
    assert placed["B"].start == pytest.approx(10.0)
    assert placed["B"].start - placed["A"].end <= 1e-9


def test_pin_fixes_machine_and_earliest_start() -> None:
    pin = {"machine_id": "M2", "earliest_start": 7.0}
    result = _solve(_problem([[_op("A", {"M1": 5.0, "M2": 5.0}, pin=pin)]]))

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].machine_id == "M2"
    assert _placed(result)["A"].start == pytest.approx(7.0)


def test_pin_latest_start_orders_the_machine() -> None:
    ops = [_op("A", {"M1": 5.0}), _op("B", {"M1": 5.0}, pin={"latest_start": 0.0})]
    result = _solve(_problem([ops]))

    _assert_optimal_and_verified(result)
    assert _placed(result)["B"].start == pytest.approx(0.0)
    assert _placed(result)["A"].start == pytest.approx(5.0)


def test_in_progress_op_runs_at_now_and_blocks_its_machine() -> None:
    running = _op(
        "X",
        {"M1": 9.0},
        status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 4.0},
    )
    waiting = _op("Y", {"M1": 5.0})
    problem = _problem([[running], [waiting]], (_machine("M1"),), now=10.0)

    result = _solve(problem)

    _assert_optimal_and_verified(result)
    placed = _placed(result)
    assert (placed["X"].start, placed["X"].end) == (pytest.approx(10.0), pytest.approx(14.0))
    assert placed["Y"].start == pytest.approx(14.0)
    assert result.schedule.primary_value == pytest.approx(9.0)


def test_machine_available_from_and_release_are_lower_bounds() -> None:
    late_machine = (_machine("M1", state={"available_from": 6.0}),)
    released = Job(id="J", release=8.0, operations=(_op("B", {"M1": 1.0}),))

    result = _solve(_problem([[_op("A", {"M1": 1.0})], released], late_machine))

    _assert_optimal_and_verified(result)
    assert _placed(result)["A"].start == pytest.approx(6.0)
    assert _placed(result)["B"].start >= 8.0 - 1e-9


def test_done_ops_and_held_jobs_are_not_scheduled() -> None:
    done = _op("A", {"M1": 5.0}, status={"state": "done"})
    after_done = _op("B", {"M1": 5.0}, predecessors=[{"operation_id": "A", "min_lag": 1.0}])
    held = Job(id="H", status="held", operations=(_op("C", {"M1": 5.0}),))

    result = _solve(_problem([[done, after_done], held]))

    _assert_optimal_and_verified(result)
    assert set(_placed(result)) == {"B"}
    assert _placed(result)["B"].start == pytest.approx(0.0)


def test_horizon_end_too_short_is_infeasible() -> None:
    result = _solve(_problem([[_op("A", {"M1": 5.0})]], horizon_end=4.0))

    assert result.schedule.status == "INFEASIBLE"
    assert not result.verified
    assert _codes(result) == ["infeasible"]


# --- objectives ----------------------------------------------------------------------------


def test_max_lateness_primary_uses_each_jobs_due() -> None:
    long_job = Job(id="L", due=100.0, operations=(_op("A", {"M1": 10.0}),))
    urgent_job = Job(id="U", due=2.0, operations=(_op("B", {"M1": 2.0}),))
    problem = _problem([long_job, urgent_job], (_machine("M1"),))

    result = _solve(problem, ObjectiveKind.MAX_LATENESS)

    _assert_optimal_and_verified(result)
    assert _placed(result)["B"].start == pytest.approx(0.0)
    assert result.schedule.primary_value == pytest.approx(0.0)
    assert result.objective_values[ObjectiveKind.MAX_LATENESS] == pytest.approx(0.0)


def test_pause_is_still_refused_through_solve() -> None:
    calendar = {"id": "c", "intervals": [(0.0, 5.0), (10.0, 30.0)]}
    problem = _problem(
        [[_op("A", {"M1": 6.0}, shift_crossing="pause")]],
        (_machine("M1", calendar_id="c"),),
        calendars=(calendar,),
    )

    result = _solve(problem)

    assert result.schedule.status == "UNKNOWN"
    assert _codes(result) == ["unsupported_feature"]
    assert "pause" in result.issues[0].message


# --- precision and time limit --------------------------------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {
            "options": [
                {
                    "machine_id": "M1",
                    "fixed_seconds": 1.0000001,
                    "unit_seconds": 0.0,
                    "preference_cost": 0.0,
                }
            ]
        },
        {"ready_time": 0.1234567},
    ],
)
def test_more_than_six_decimals_is_refused(fields: dict[str, Any]) -> None:
    result = _solve(_problem([[_op("A", **fields)]]))

    assert result.schedule.status == "UNKNOWN"
    assert _codes(result) == ["unsupported_precision"]


def test_six_decimals_are_accepted() -> None:
    result = _solve(_problem([[_op("A", {"M1": 1.000001})]]))

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(1.000001)


def _random_job_shop(seed: int, jobs: int, machines: int) -> Problem:
    """A job shop where every op may run on two random machines with random times."""
    rng = np.random.default_rng(seed)
    machine_ids = [f"M{k}" for k in range(machines)]
    built: list[list[Operation]] = []
    for j in range(jobs):
        ops: list[Operation] = []
        for k, m in enumerate(rng.permutation(machines)):
            other = int(rng.integers(machines))
            runs = {machine_ids[m]: float(rng.integers(1, 50))}
            runs.setdefault(machine_ids[other], float(rng.integers(1, 50)))
            links = [{"operation_id": f"{j}-{k - 1}"}] if k else []
            ops.append(_op(f"{j}-{k}", runs, predecessors=links))
        built.append(ops)
    return _problem(built, tuple(_machine(m) for m in machine_ids))


def test_tiny_time_limit_never_returns_an_unverified_plan() -> None:
    request = _request(_random_job_shop(seed=7, jobs=20, machines=10))

    result = solve(request, solver=ORToolsSolver(), budget_seconds=1e-3)

    assert result.schedule.status in {"UNKNOWN", "FEASIBLE"}
    if result.schedule.status == "FEASIBLE":
        assert result.verified, result.issues
    else:
        assert result.schedule.assignments == ()
        assert _codes(result) == ["time_limit"]


def test_zero_length_op_never_sits_inside_another_op_or_a_break() -> None:
    calendar = {"id": "c", "intervals": [(0.0, 5.0), (10.0, 30.0)]}
    ops = [_op("A", {"M1": 5.0}), _op("Z", {"M1": 0.0})]
    problem = _problem([ops], (_machine("M1", calendar_id="c"),), calendars=(calendar,))

    result = _solve(problem)

    _assert_optimal_and_verified(result)


# --- magnitudes CP-SAT cannot hold in int64 ticks (TF-002 review) ----------------------------


def _huge_unit_op() -> Operation:
    op = _op("A")
    option = op.options[0].model_copy(update={"unit_seconds": 1.0})
    return op.model_copy(update={"quantity": 1e15, "options": (option,)})


_HUGE: dict[str, Problem] = {
    "release": _problem([Job(id="J", release=1e13, operations=(_op("A"),))]),
    "max_lag": _problem(
        [[_op("A"), _op("B", predecessors=[{"operation_id": "A", "max_lag": 1e14}])]]
    ),
    "due": _problem([Job(id="J", due=4e12, operations=(_op("A"),))]),
    "durations": _problem([[_op("A", {"M1": 3e12})], [_op("B", {"M1": 3e12})]]),
    "calendar": _problem(
        [[_op("A")]],
        (_machine("M1", calendar_id="c"),),
        calendars=({"id": "c", "intervals": [(1e13, 1e13 + 10.0)]},),
    ),
    "pin": _problem([[_op("A", pin={"latest_start": 1e14})]]),
    "quantity": _problem([[_huge_unit_op()]]),
}


@pytest.mark.parametrize("name", sorted(_HUGE))
def test_values_too_large_for_int64_ticks_are_refused_as_unsupported_magnitude(
    name: str,
) -> None:
    for kind in (ObjectiveKind.MAKESPAN, ObjectiveKind.MAX_LATENESS):
        result = _solve(_HUGE[name], kind)

        assert result.schedule.status == "UNKNOWN"
        assert _codes(result) == ["unsupported_magnitude"]


def test_epoch_scale_times_are_still_solved() -> None:
    now = 1.8e9  # seconds since 1970, around 2027
    problem = _problem([Job(id="J", release=now, due=now + 100.0, operations=(_op("A"),))], now=now)

    _assert_optimal_and_verified(_solve(problem))


# --- shared helpers for CP-SAT v3 (T-134, T-135) ---------------------------------------------

Values = dict[ObjectiveKind, float]
EXACT = 1e-9
"""Tick-exact comparison: one CP-SAT tick is 1e-6 s, so `pytest.approx`'s default
relative tolerance would hide an off-by-one-tick answer."""
ORACLE_LIMIT = 10_000
ALL_KINDS = tuple(ObjectiveKind)


def _exact_solver() -> Any:
    return get_solver("ortools", workers=1, seed=0)


def _single(kind: ObjectiveKind) -> Lexicographic:
    return Lexicographic(levels=(LexicographicLevel(kind=kind),))


def _solve_spec(
    problem: Problem, spec: ObjectiveSpec, previous_plan: PreviousPlan | None = None
) -> SolveResult:
    profile = SchedulingProfile(objective=spec)
    request = SolveRequest(
        schema_version="2.0", problem=problem, profile=profile, previous_plan=previous_plan
    )
    return solve(request, solver=_exact_solver(), budget_seconds=30.0)


def _oracle_values(problem: Problem, previous_plan: PreviousPlan | None = None) -> list[Values]:
    """Every semi-active schedule's objective values, by the independent `oracle_math`."""
    assert combination_count(problem, ORACLE_LIMIT) <= ORACLE_LIMIT
    compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
    found: list[Values] = []
    for sequences in all_sequences(problem):
        try:
            schedule = semi_active(problem, sequences)
        except SequenceDeadlock:
            continue
        found.append(objective_values(compiled, schedule, previous_plan))
    return found


def _to_minimize(kind: ObjectiveKind, value: float) -> float:
    return -value if kind is ObjectiveKind.ROBUSTNESS else value


def _oracle_best(found: Sequence[Values], kind: ObjectiveKind) -> float:
    best = min(_to_minimize(kind, values[kind]) for values in found)
    return _to_minimize(kind, best)


def _weighted_score(spec: WeightedSum, values: Values) -> float:
    """sum weight * value / scale, robustness negated (every test term carries a scale)."""
    return math.fsum(
        term.weight * _to_minimize(term.kind, values[term.kind]) / (term.scale or 1.0)
        for term in spec.terms
    )


def _assert_optimal_verified(result: SolveResult) -> None:
    assert result.schedule.status == "OPTIMAL", result.issues
    assert result.verified, result.issues


def _priced_op(op_id: str, runs: dict[str, tuple[float, float]], **fields: Any) -> Operation:
    """An operation with (fixed seconds, preference cost) per machine."""
    options = [
        {"machine_id": m, "fixed_seconds": s, "unit_seconds": 0.0, "preference_cost": cost}
        for m, (s, cost) in runs.items()
    ]
    data = {"id": op_id, "work_center_id": WC, "quantity": 1.0, "options": options}
    return Operation.model_validate(data | fields)


def _mixed_problem(dues: tuple[float, float, float, float] = (6.0, 5.0, 9.0, 4.0)) -> Problem:
    """Four weighted jobs with dues on two priced machines: alternatives, a release, a lag."""
    a0 = _priced_op("a0", {"M1": (4.0, 1.0), "M2": (3.0, 2.0)})
    a1 = _priced_op("a1", {"M1": (2.0, 0.5)}, predecessors=[{"operation_id": "a0", "min_lag": 1.0}])
    b0 = _priced_op("b0", {"M1": (3.0, 3.0), "M2": (5.0, 1.0)})
    c0 = _priced_op("c0", {"M1": (6.0, 2.0), "M2": (4.0, 4.0)})
    d0 = _priced_op("d0", {"M2": (2.0, 0.25)})
    jobs = [
        Job(id="J0", weight_override=1.0, due=dues[0], operations=(a0, a1)),
        Job(id="J1", weight_override=3.0, due=dues[1], operations=(b0,)),
        Job(id="J2", weight_override=2.0, due=dues[2], release=2.0, operations=(c0,)),
        Job(id="J3", weight_override=1.0, due=dues[3], operations=(d0,)),
    ]
    machines = (_machine("M1", hour_cost=36.0), _machine("M2", hour_cost=72.0))
    return _problem(jobs, machines)


def _previous_plan(problem: Problem) -> PreviousPlan:
    """A poor but feasible plan of `_mixed_problem`: c0 first on M1, b0 on M2."""
    schedule = semi_active(problem, {"M1": ("c0", "a0", "a1"), "M2": ("d0", "b0")})
    assignments = [
        {"operation_id": a.operation_id, "machine_id": a.machine_id, "start": a.start}
        for a in schedule.assignments
    ]
    return PreviousPlan.model_validate(
        {"plan_id": "prev", "problem_digest": "d", "assignments": assignments}
    )


def _kind_case(kind: ObjectiveKind) -> tuple[Problem, PreviousPlan | None]:
    """The instance for a single-kind test; semi-active schedules contain its optimum.

    Earliness-tardiness is not regular, so its case has every due at 0 (no job can be
    early) and the oracle stays exact; `test_earliness_tardiness_waits_for_a_late_due`
    covers the early side. Churn needs a previous plan; it is one semi-active schedule.
    """
    if kind is ObjectiveKind.EARLINESS_TARDINESS:
        return _mixed_problem(dues=(0.0, 0.0, 0.0, 0.0)), None
    problem = _mixed_problem()
    if kind is ObjectiveKind.CHURN:
        return problem, _previous_plan(problem)
    return problem, None


def _late_or_long() -> Problem:
    """One machine: a long job L (no due) and an urgent job U released at 5, due 6, weight 10.

    L first: makespan 11, weighted tardiness 50. U first: makespan 16, weighted tardiness 0.
    """
    long_job = Job(id="L", operations=(_op("l", {"M1": 10.0}),))
    urgent = Job(
        id="U", release=5.0, due=6.0, weight_override=10.0, operations=(_op("u", {"M1": 1.0}),)
    )
    return _problem([long_job, urgent], (_machine("M1"),))


# --- TF-002 review item 2: zero-length runs ---------------------------------------------------


def test_zero_length_op_with_zero_max_lag_is_not_falsely_infeasible() -> None:
    """A then B on one machine, B within 0 s of A's end: A's one-tick hold made it INFEASIBLE."""
    zero = _op("A", {"M1": 0.0})
    after = _op("B", {"M1": 5.0}, predecessors=[{"operation_id": "A", "max_lag": 0.0}])
    problem = _problem([[zero, after]], (_machine("M1"),))

    result = _solve_spec(problem, _single(ObjectiveKind.MAKESPAN))

    _assert_optimal_verified(result)
    placed = _placed(result)
    assert placed["A"].end == pytest.approx(0.0, abs=EXACT)
    assert placed["B"].start == pytest.approx(0.0, abs=EXACT)
    assert result.objective_values[ObjectiveKind.MAKESPAN] == pytest.approx(5.0, abs=EXACT)


def test_zero_length_op_in_a_chain_adds_no_tick_to_its_successor() -> None:
    """A(5) -> Z(0) -> B(5) on one machine is 10 s, not 10.000001 (the "5.000001" artifact)."""
    ops = [
        _op("A", {"M1": 5.0}),
        _op("Z", {"M1": 0.0}, predecessors=[{"operation_id": "A"}]),
        _op("B", {"M1": 5.0}, predecessors=[{"operation_id": "Z"}]),
    ]
    problem = _problem([ops], (_machine("M1"),))

    result = _solve_spec(problem, _single(ObjectiveKind.MAKESPAN))

    _assert_optimal_verified(result)
    assert _placed(result)["B"].start == pytest.approx(5.0, abs=EXACT)
    assert result.schedule.primary_value == pytest.approx(10.0, abs=EXACT)
    assert result.objective_values[ObjectiveKind.MAKESPAN] == pytest.approx(10.0, abs=EXACT)
    assert result.schedule.best_bound == pytest.approx(10.0, abs=EXACT)


# --- TF-002 review item 8 and T-134: objective specs -------------------------------------------


_WEIGHTED_SUMS = {
    "tardiness_first": WeightedSum(
        terms=(
            WeightedTerm(kind=ObjectiveKind.WEIGHTED_TARDINESS, weight=1.0, scale=25.0),
            WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=1.0, scale=1.0),
        )
    ),
    "makespan_first": WeightedSum(
        terms=(
            WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=1.0, scale=1.0),
            WeightedTerm(kind=ObjectiveKind.WEIGHTED_TARDINESS, weight=1.0, scale=5.0),
        )
    ),
}


@pytest.mark.parametrize("name", sorted(_WEIGHTED_SUMS))
def test_weighted_sum_optimizes_the_whole_sum_not_the_first_term(name: str) -> None:
    spec = _WEIGHTED_SUMS[name]
    problem = _late_or_long()
    found = _oracle_values(problem)
    optimum = min(_weighted_score(spec, values) for values in found)
    first = spec.terms[0].kind
    first_best = _oracle_best(found, first)
    first_only = [v for v in found if abs(v[first] - first_best) <= EXACT]
    assert min(_weighted_score(spec, v) for v in first_only) > optimum + 1.0  # discriminates

    result = _solve_spec(problem, spec)

    _assert_optimal_verified(result)
    assert _weighted_score(spec, result.objective_values) == pytest.approx(optimum, abs=1e-6)


@pytest.mark.parametrize("kind", ALL_KINDS, ids=[k.value for k in ALL_KINDS])
def test_single_kind_objective_reaches_the_oracle_optimum(kind: ObjectiveKind) -> None:
    problem, previous = _kind_case(kind)
    optimum = _oracle_best(_oracle_values(problem, previous), kind)

    result = _solve_spec(problem, _single(kind), previous)

    _assert_optimal_verified(result)
    assert result.objective_values[kind] == pytest.approx(optimum, abs=1e-6)
    assert result.schedule.primary_value == pytest.approx(optimum, abs=1e-6)


@pytest.mark.parametrize(
    "kind",
    [
        ObjectiveKind.WEIGHTED_TARDINESS,
        ObjectiveKind.WEIGHTED_COMPLETION,
        ObjectiveKind.ROBUSTNESS,
        ObjectiveKind.COST,
    ],
)
def test_best_bound_equals_the_optimum_on_optimal(kind: ObjectiveKind) -> None:
    problem, previous = _kind_case(kind)
    optimum = _oracle_best(_oracle_values(problem, previous), kind)

    result = _solve_spec(problem, _single(kind), previous)

    _assert_optimal_verified(result)
    assert result.schedule.best_bound == pytest.approx(optimum, abs=1e-6)


def test_non_regular_churn_case_is_not_won_by_the_earliest_plan() -> None:
    """Design guard: the churn optimum 0 needs the previous plan, not the makespan plan."""
    problem, previous = _kind_case(ObjectiveKind.CHURN)
    found = _oracle_values(problem, previous)
    best_makespan = _oracle_best(found, ObjectiveKind.MAKESPAN)
    fastest = [v for v in found if abs(v[ObjectiveKind.MAKESPAN] - best_makespan) <= EXACT]

    assert _oracle_best(found, ObjectiveKind.CHURN) == 0.0
    assert min(v[ObjectiveKind.CHURN] for v in fastest) > 0.0


def test_earliness_tardiness_waits_for_a_late_due() -> None:
    """E/T is two-sided: finishing early costs, so the optimum inserts idle (value 0)."""
    jobs = [
        Job(id="A", due=10.0, operations=(_op("a", {"M1": 5.0}),)),
        Job(id="B", due=4.0, operations=(_op("b", {"M1": 3.0}),)),
    ]
    problem = _problem(jobs, (_machine("M1"),))

    result = _solve_spec(problem, _single(ObjectiveKind.EARLINESS_TARDINESS))

    _assert_optimal_verified(result)
    assert result.objective_values[ObjectiveKind.EARLINESS_TARDINESS] == pytest.approx(
        0.0, abs=EXACT
    )
    assert _placed(result)["a"].end == pytest.approx(10.0, abs=EXACT)
    assert _placed(result)["b"].end == pytest.approx(4.0, abs=EXACT)


def test_lexicographic_tardiness_then_makespan_matches_the_oracle() -> None:
    problem = _mixed_problem(dues=(9.0, 8.0, 12.0, 6.0))
    found = _oracle_values(problem)
    tardiness = _oracle_best(found, ObjectiveKind.WEIGHTED_TARDINESS)
    tied = [v for v in found if v[ObjectiveKind.WEIGHTED_TARDINESS] <= tardiness + EXACT]
    makespan = min(v[ObjectiveKind.MAKESPAN] for v in tied)
    assert max(v[ObjectiveKind.MAKESPAN] for v in tied) > makespan  # the second level matters
    spec = Lexicographic(
        levels=(
            LexicographicLevel(kind=ObjectiveKind.WEIGHTED_TARDINESS),
            LexicographicLevel(kind=ObjectiveKind.MAKESPAN),
        )
    )

    result = _solve_spec(problem, spec)

    _assert_optimal_verified(result)
    values = result.objective_values
    assert values[ObjectiveKind.WEIGHTED_TARDINESS] == pytest.approx(tardiness, abs=1e-6)
    assert values[ObjectiveKind.MAKESPAN] == pytest.approx(makespan, abs=1e-6)


def test_epsilon_bound_is_respected_and_the_primary_optimized_under_it() -> None:
    problem = _late_or_long()
    found = _oracle_values(problem)
    upper = 0.0
    allowed = [v for v in found if v[ObjectiveKind.WEIGHTED_TARDINESS] <= upper + EXACT]
    makespan = min(v[ObjectiveKind.MAKESPAN] for v in allowed)
    assert makespan > _oracle_best(found, ObjectiveKind.MAKESPAN)  # the bound binds
    spec = EpsilonConstraint(
        primary=ObjectiveKind.MAKESPAN,
        bounds=(EpsilonBound(kind=ObjectiveKind.WEIGHTED_TARDINESS, upper=upper),),
    )

    result = _solve_spec(problem, spec)

    _assert_optimal_verified(result)
    assert result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS] <= upper + 1e-6
    assert result.objective_values[ObjectiveKind.MAKESPAN] == pytest.approx(makespan, abs=1e-6)


def test_a_weight_that_rounds_to_a_zero_coefficient_is_unsupported_precision() -> None:
    spec = WeightedSum(
        terms=(
            WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=1.0, scale=1.0),
            WeightedTerm(kind=ObjectiveKind.WEIGHTED_TARDINESS, weight=1e-12, scale=1.0),
        )
    )

    result = _solve_spec(_late_or_long(), spec)

    assert result.schedule.status == "UNKNOWN"
    assert result.schedule.assignments == ()
    assert _codes(result) == ["unsupported_precision"]


# --- T-134: the solver reads context.deadline -------------------------------------------------


def _context(deadline: float) -> SolveContext:
    return SolveContext(
        previous_plan=None, churn_tolerance=0.0, deadline=deadline, warm_start=None, params={}
    )


def _assert_stopped_cleanly(problem: Problem, deadline: float, budget: float) -> float:
    """Run directly with `deadline` and a larger `budget`; return the elapsed seconds."""
    started = time.monotonic()
    schedule = _exact_solver().solve(
        problem,
        objective=_single(ObjectiveKind.MAKESPAN),
        budget_seconds=budget,
        context=_context(deadline),
    )
    elapsed = time.monotonic() - started
    assert schedule.status in {"UNKNOWN", "FEASIBLE"}
    if schedule.status == "UNKNOWN":
        assert [issue.code for issue in schedule.issues] == ["time_limit"]
    else:
        compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
        assert verify(compiled, (), schedule).valid
    return elapsed


def test_a_deadline_already_passed_returns_at_once() -> None:
    """12x6 alternatives job shop: CP-SAT runs its whole budget on it (measured 10 s / 10 s)."""
    problem = _random_job_shop(seed=7, jobs=12, machines=6)

    elapsed = _assert_stopped_cleanly(problem, time.monotonic() - 1.0, budget=5.0)

    assert elapsed < 1.0


def test_the_deadline_wins_over_a_longer_budget() -> None:
    problem = _random_job_shop(seed=7, jobs=12, machines=6)

    elapsed = _assert_stopped_cleanly(problem, time.monotonic() + 0.5, budget=5.0)

    assert elapsed < 2.0


# --- T-135: sequence-dependent setups, machine state, detached setups -------------------------


def test_pinedo_14_2_1_weighted_tardiness_is_optimal_at_98() -> None:
    request = SolveRequest.model_validate_json((FIXTURES / "pinedo_14_2_1.json").read_text())

    result = solve(request, solver=_exact_solver(), budget_seconds=30.0)

    _assert_optimal_verified(result)
    assert result.objective_values[ObjectiveKind.WEIGHTED_TARDINESS] == pytest.approx(98.0)
    assert result.schedule.primary_value == pytest.approx(98.0)
    assert result.schedule.best_bound == pytest.approx(98.0)


def test_pinedo_14_2_1_total_setup_equals_the_brute_force_minimum() -> None:
    request = SolveRequest.model_validate_json((FIXTURES / "pinedo_14_2_1.json").read_text())
    oracle_value, _ = brute_force(request.problem, ObjectiveKind.TOTAL_SETUP)
    profile = SchedulingProfile(objective=_single(ObjectiveKind.TOTAL_SETUP))

    result = solve(
        request.model_copy(update={"profile": profile}),
        solver=_exact_solver(),
        budget_seconds=30.0,
    )

    _assert_optimal_verified(result)
    assert result.objective_values[ObjectiveKind.TOTAL_SETUP] == pytest.approx(oracle_value)


def _state_problem(last_w: float, *, detached: bool = False, release: float = 0.0) -> Problem:
    """One op with setup param w=4 on a machine whose last w is `last_w` (1 s per unit)."""
    function = {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
    work = _op("A", {"M1": 5.0}, setup_params={"w": 4.0}, setup_detached=detached)
    machine = _machine("M1", setup_function_id="S", state={"last_setup_params": {"w": last_w}})
    job = Job(id="J", release=release, operations=(work,))
    return _problem([job], (machine,), setup_functions=(function,))


@pytest.mark.parametrize(("last_w", "setup"), [(0.0, 4.0), (1.0, 3.0), (4.0, 0.0)])
def test_machine_state_last_params_set_the_first_setup(last_w: float, setup: float) -> None:
    result = _solve_spec(_state_problem(last_w), _single(ObjectiveKind.MAKESPAN))

    _assert_optimal_verified(result)
    placed = _placed(result)["A"]
    assert placed.start == pytest.approx(0.0, abs=EXACT)
    assert placed.setup_end - placed.start == pytest.approx(setup, abs=EXACT)
    assert placed.end == pytest.approx(setup + 5.0, abs=EXACT)


def test_detached_setup_runs_before_the_release_and_beats_attached() -> None:
    detached = _solve_spec(
        _state_problem(0.0, detached=True, release=10.0), _single(ObjectiveKind.MAKESPAN)
    )
    attached = _solve_spec(_state_problem(0.0, release=10.0), _single(ObjectiveKind.MAKESPAN))

    _assert_optimal_verified(detached)
    _assert_optimal_verified(attached)
    early = _placed(detached)["A"]
    assert (early.start, early.setup_end, early.end) == (
        pytest.approx(6.0, abs=EXACT),
        pytest.approx(10.0, abs=EXACT),
        pytest.approx(15.0, abs=EXACT),
    )
    late = _placed(attached)["A"]
    assert late.start == pytest.approx(10.0, abs=EXACT)
    assert late.end == pytest.approx(19.0, abs=EXACT)


def _setup_params(rng: np.random.Generator) -> dict[str, Any]:
    return {"w": float(rng.integers(0, 5)), "c": str(rng.choice(["red", "blue"]))}


def _setup_job(rng: np.random.Generator, j: int) -> Job:
    ops: list[Operation] = []
    for k in range(int(rng.integers(1, 3))):
        machines = ["M1", "M2"] if rng.random() < 0.5 else [("M1", "M2")[int(rng.integers(2))]]
        runs = {m: float(rng.integers(1, 8)) for m in machines}
        links = [{"operation_id": ops[-1].id, "min_lag": float(rng.integers(0, 3))}] if ops else []
        detached = bool(rng.random() < 0.4)
        params = _setup_params(rng)
        ops.append(
            _op(
                f"J{j}-{k}",
                runs,
                predecessors=links,
                setup_params=params,
                setup_detached=detached,
            )
        )
    return Job(
        id=f"J{j}",
        release=float(rng.integers(0, 6)),
        due=float(rng.integers(6, 25)),
        weight_override=float(rng.integers(1, 4)),
        operations=tuple(ops),
    )


def _setup_shop(seed: int) -> Problem:
    """3 jobs of 1-2 ops on 2 machines (<= 5,040 oracle combinations) with abs_diff plus
    mismatch setups on the work center, a last-params machine state per machine, random
    detached setups, min lags, releases, dues and weights."""
    rng = np.random.default_rng((seed, 135))
    jobs = [_setup_job(rng, j) for j in range(3)]
    machines = tuple(
        _machine(m, state={"last_setup_params": _setup_params(rng)}) for m in ("M1", "M2")
    )
    function = {
        "id": "S",
        "terms": [
            {"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0},
            {"kind": "mismatch", "param": "c", "seconds": 2.0},
        ],
    }
    return _problem(
        jobs,
        machines,
        work_centers=(WorkCenter(id=WC, setup_function_id="S"),),
        setup_functions=(function,),
    )


def _with_detached(problem: Problem, detached: bool) -> Problem:
    jobs = tuple(
        job.model_copy(
            update={
                "operations": tuple(
                    op.model_copy(update={"setup_detached": detached}) for op in job.operations
                )
            }
        )
        for job in problem.jobs
    )
    return problem.model_copy(update={"jobs": jobs})


_SEEDS = range(20)
_SETUP_KINDS = (ObjectiveKind.WEIGHTED_TARDINESS, ObjectiveKind.MAKESPAN)


@pytest.mark.parametrize("kind", _SETUP_KINDS, ids=[k.value for k in _SETUP_KINDS])
@pytest.mark.parametrize("seed", _SEEDS)
def test_random_setup_shops_reach_the_oracle_optimum(seed: int, kind: ObjectiveKind) -> None:
    problem = _setup_shop(seed)
    assert combination_count(problem, ORACLE_LIMIT) <= ORACLE_LIMIT
    oracle_value, _ = brute_force(problem, kind)

    result = _solve_spec(problem, _single(kind))

    _assert_optimal_verified(result)
    assert result.objective_values[kind] == pytest.approx(oracle_value, abs=1e-6)


def test_random_setup_shops_use_setups_and_detached_setups() -> None:
    """Design guard: the generator really exercises the T-135 features."""
    features = [
        set(_solve_spec(_setup_shop(seed), _single(ObjectiveKind.MAKESPAN)).features)
        for seed in _SEEDS
    ]
    assert all({"setups", "machine_state"} <= used for used in features)
    assert sum("detached_setup" in used for used in features) >= 10


@pytest.mark.parametrize("seed", range(10))
def test_detached_setups_are_never_worse_than_attached(seed: int) -> None:
    problem = _setup_shop(seed)
    makespan = _single(ObjectiveKind.MAKESPAN)

    detached = _solve_spec(_with_detached(problem, True), makespan)
    attached = _solve_spec(_with_detached(problem, False), makespan)

    _assert_optimal_verified(detached)
    _assert_optimal_verified(attached)
    detached_value = detached.objective_values[ObjectiveKind.MAKESPAN]
    assert detached_value <= attached.objective_values[ObjectiveKind.MAKESPAN] + EXACT
