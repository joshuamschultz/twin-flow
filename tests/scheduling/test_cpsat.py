"""CP-SAT adapter tests (T-103, COMP-010, REQ-076, REQ-078, D-067, D-068).

Every plan goes through `solver.solve`, so the adapter's answers are checked by the
independent verifier and evaluated, not trusted. The module is skipped when the optional
`ortools` extra is absent.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("ortools")

from oracle import brute_force  # noqa: E402

from twinflow.scheduling.algorithms.cpsat import ORToolsSolver  # noqa: E402
from twinflow.scheduling.model import (  # noqa: E402
    Assignment,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    Operation,
    Problem,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    SolveResult,
    WorkCenter,
)
from twinflow.scheduling.seam import Feature, OptionalDependencyError  # noqa: E402
from twinflow.scheduling.solver import solve  # noqa: E402

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
    ops = [_op("A", {"M1": 3.0, "M2": 6.0}), _op("B", {"M1": 3.0, "M2": 6.0})]
    result = _solve(_problem([ops]))

    _assert_optimal_and_verified(result)
    assert result.schedule.primary_value == pytest.approx(6.0)
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


def test_weighted_tardiness_primary_is_unsupported_and_not_run() -> None:
    result = _solve(_problem([[_op("A")]]), ObjectiveKind.WEIGHTED_TARDINESS)

    assert result.schedule.status == "UNKNOWN"
    assert result.schedule.assignments == ()
    assert not result.verified
    assert _codes(result) == ["unsupported_objective"]


def test_setups_and_detached_setup_are_refused_through_solve() -> None:
    setup_fn = SetupFunction(id="s", terms=(), default_seconds=3.0)
    problem = _problem(
        [[_op("A", {"M1": 5.0}, setup_detached=True)]],
        (_machine("M1", setup_function_id="s"),),
        setup_functions=(setup_fn,),
    )

    result = _solve(problem)

    assert result.schedule.status == "UNKNOWN"
    assert _codes(result) == ["unsupported_feature", "unsupported_feature"]
    messages = " ".join(issue.message for issue in result.issues)
    assert "detached_setup" in messages
    assert "feature setups" in messages


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
