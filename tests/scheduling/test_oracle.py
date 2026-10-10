"""Tests for the brute-force oracle and the two Pinedo fixtures (T-090, COMP-012).

Example 7.1.1 (p.182, J4 || Cmax) has optimum makespan 28 (Example 7.1.4, p.189).
Example 14.2.1 (p.376-377, 1 | s_jk | sum w_j T_j) has optimum 98 at 2, 4, 3, 1.
Requirements: REQ-078, REQ-079.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from oracle import (
    MAX_COMBINATIONS,
    SequenceDeadlock,
    brute_force,
    combination_count,
    semi_active,
)

from twinflow.scheduling.model import (
    Assignment,
    Calendar,
    CompiledProblem,
    Job,
    Machine,
    ObjectiveKind,
    Operation,
    Precedence,
    Problem,
    ProcessingOption,
    Schedule,
    SchedulingProfile,
    SolveRequest,
    WorkCenter,
)
from twinflow.scheduling.objectives import evaluate

FIXTURES = Path(__file__).parent / "fixtures"
K = ObjectiveKind


def _load(name: str) -> SolveRequest:
    return SolveRequest.model_validate_json((FIXTURES / name).read_text())


def _by_op(schedule: Schedule) -> dict[str, Assignment]:
    return {a.operation_id: a for a in schedule.assignments}


def _option(machine_id: str, seconds: float) -> ProcessingOption:
    return ProcessingOption(
        machine_id=machine_id, fixed_seconds=seconds, unit_seconds=0.0, preference_cost=0.0
    )


def _op(op_id: str, *machines: str, seconds: float = 5.0, **fields: object) -> Operation:
    return Operation.model_validate(
        {
            "id": op_id,
            "work_center_id": "WC",
            "quantity": 1.0,
            "options": [_option(m, seconds) for m in machines],
            **fields,
        }
    )


def _problem(*jobs: Job, machines: tuple[Machine, ...] | None = None, **fields: object) -> Problem:
    default_machines = (Machine(id="A", work_center_id="WC"), Machine(id="B", work_center_id="WC"))
    return Problem.model_validate(
        {
            "id": "p",
            "jobs": jobs,
            "machines": machines or default_machines,
            "work_centers": (WorkCenter(id="WC"),),
            **fields,
        }
    )


# --- fixtures --------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["pinedo_7_1_1.json", "pinedo_14_2_1.json"])
def test_fixture_parses_as_v2_solve_request(name: str) -> None:
    request = _load(name)

    assert request.schema_version == "2.0"


def test_fixture_7_1_1_encodes_the_pinedo_routes() -> None:
    problem = _load("pinedo_7_1_1.json").problem

    routes = {
        job.id: [(op.options[0].machine_id, op.options[0].fixed_seconds) for op in job.operations]
        for job in problem.jobs
    }

    assert routes == {
        "1": [("M1", 10), ("M2", 8), ("M3", 4)],
        "2": [("M2", 8), ("M1", 3), ("M4", 5), ("M3", 6)],
        "3": [("M1", 4), ("M2", 7), ("M4", 3)],
    }


def test_combination_count_matches_the_plan_for_both_fixtures() -> None:
    assert combination_count(_load("pinedo_7_1_1.json").problem) == 144
    assert combination_count(_load("pinedo_14_2_1.json").problem) == 24


# --- Pinedo 7.1.1 ------------------------------------------------------------------------------


def test_brute_force_7_1_1_makespan_optimum_is_28() -> None:
    problem = _load("pinedo_7_1_1.json").problem

    value, schedule = brute_force(problem, K.MAKESPAN)

    assert value == 28.0
    assert max(a.end for a in schedule.assignments) == 28.0
    assert schedule.solver == "oracle"


def test_brute_force_7_1_1_schedule_honors_routes_and_machines() -> None:
    problem = _load("pinedo_7_1_1.json").problem

    _, schedule = brute_force(problem, K.MAKESPAN)

    placed = _by_op(schedule)
    assert len(placed) == 10
    for job in problem.jobs:
        for earlier, later in itertools.pairwise(job.operations):
            assert placed[later.id].start >= placed[earlier.id].end
    for machine in problem.machines:
        on_machine = sorted(
            (a for a in schedule.assignments if a.machine_id == machine.id), key=lambda a: a.start
        )
        for first, second in itertools.pairwise(on_machine):
            assert second.start >= first.end


# --- Pinedo 14.2.1 -----------------------------------------------------------------------------


def _weighted_tardiness_by_sequence(request: SolveRequest) -> dict[tuple[str, ...], float]:
    compiled = CompiledProblem(problem=request.problem, profile=request.profile)
    ops = [job.operations[0].id for job in request.problem.jobs]
    values = {}
    for order in itertools.permutations(ops):
        schedule = semi_active(request.problem, {"M1": order})
        values[order] = evaluate(compiled, schedule)[K.WEIGHTED_TARDINESS]
    return values


def test_brute_force_14_2_1_weighted_tardiness_optimum_is_98_at_2_4_3_1() -> None:
    request = _load("pinedo_14_2_1.json")

    value, schedule = brute_force(request.problem, K.WEIGHTED_TARDINESS, request.profile)

    order = [a.operation_id for a in sorted(schedule.assignments, key=lambda a: a.start)]
    assert value == 98.0
    assert order == ["2-1", "4-1", "3-1", "1-1"]


def test_brute_force_14_2_1_optimum_completions_are_10_20_34_48() -> None:
    request = _load("pinedo_14_2_1.json")

    _, schedule = brute_force(request.problem, K.WEIGHTED_TARDINESS, request.profile)

    ends = {op_id: a.end for op_id, a in _by_op(schedule).items()}
    assert ends == {"2-1": 10.0, "4-1": 20.0, "3-1": 34.0, "1-1": 48.0}


def test_semi_active_14_2_1_optimum_is_unique_and_next_best_is_101_at_1_4_2_3() -> None:
    values = _weighted_tardiness_by_sequence(_load("pinedo_14_2_1.json"))

    ranked = sorted(values.items(), key=lambda item: item[1])

    assert ranked[0] == (("2-1", "4-1", "3-1", "1-1"), 98.0)
    assert ranked[1] == (("1-1", "4-1", "2-1", "3-1"), 101.0)


def test_semi_active_14_2_1_setups_come_from_the_family_matrix() -> None:
    request = _load("pinedo_14_2_1.json")

    schedule = semi_active(request.problem, {"M1": ["2-1", "4-1", "3-1", "1-1"]})

    setups = [a.setup_end - a.start for a in schedule.assignments]
    assert sorted(setups) == [0.0, 1.0, 1.0, 1.0]
    assert _by_op(schedule)["2-1"].setup_end == 1.0


# --- semi_active semantics ---------------------------------------------------------------------


def test_semi_active_attached_setup_waits_for_predecessor_plus_min_lag() -> None:
    job = Job(
        id="j",
        operations=(
            _op("a", "A", seconds=4.0),
            _op("b", "B", seconds=2.0, predecessors=[Precedence(operation_id="a", min_lag=3.0)]),
        ),
    )
    problem = _problem(job)

    placed = _by_op(semi_active(problem, {"A": ["a"], "B": ["b"]}))

    assert (placed["b"].start, placed["b"].end) == (7.0, 9.0)


def test_semi_active_detached_setup_runs_ahead_and_only_setup_end_waits() -> None:
    setup_fn = {"id": "fam", "terms": [], "default_seconds": 5.0}
    job = Job(
        id="j",
        release=10.0,
        operations=(_op("a", "A", seconds=2.0, setup_detached=True),),
    )
    problem = _problem(
        job,
        machines=(Machine(id="A", work_center_id="WC", setup_function_id="fam"),),
        setup_functions=[setup_fn],
    )

    placed = _by_op(semi_active(problem, {"A": ["a"]}))["a"]

    assert (placed.start, placed.setup_end, placed.end) == (5.0, 10.0, 12.0)


def test_semi_active_respects_now_and_machine_available_from() -> None:
    machine = Machine.model_validate(
        {"id": "A", "work_center_id": "WC", "state": {"available_from": 20.0}}
    )
    problem = _problem(Job(id="j", operations=(_op("a", "A"),)), machines=(machine,), now=5.0)

    placed = _by_op(semi_active(problem, {"A": ["a"]}))["a"]

    assert placed.start == 20.0


def test_semi_active_runs_quantity_times_unit_seconds_plus_fixed() -> None:
    option = ProcessingOption(
        machine_id="A", fixed_seconds=3.0, unit_seconds=2.0, preference_cost=0.0
    )
    op = Operation(id="a", work_center_id="WC", quantity=4.0, options=(option,))
    problem = _problem(Job(id="j", operations=(op,)))

    assert _by_op(semi_active(problem, {"A": ["a"]}))["a"].end == 11.0


def test_semi_active_raises_deadlock_when_sequence_contradicts_precedence() -> None:
    job = Job(
        id="j",
        operations=(_op("a", "A"), _op("b", "A", predecessors=[Precedence(operation_id="a")])),
    )

    with pytest.raises(SequenceDeadlock):
        semi_active(_problem(job), {"A": ["b", "a"]})


@pytest.mark.parametrize(
    "sequences",
    [
        {"A": ["a"]},  # b missing
        {"A": ["a", "b", "a"]},  # a twice
        {"A": ["a"], "B": ["b", "zzz"]},  # unknown operation
        {"A": ["a"], "C": ["b"]},  # unknown machine
    ],
)
def test_semi_active_rejects_sequences_not_covering_each_operation_once(
    sequences: dict[str, list[str]],
) -> None:
    job = Job(id="j", operations=(_op("a", "A", "B"), _op("b", "A", "B")))

    with pytest.raises(ValueError, match="sequence"):
        semi_active(_problem(job), sequences)


def test_semi_active_rejects_machine_that_is_not_an_option() -> None:
    job = Job(id="j", operations=(_op("a", "A"),))

    with pytest.raises(ValueError, match="option"):
        semi_active(_problem(job), {"B": ["a"]})


def test_semi_active_refuses_calendars_loudly() -> None:
    machine = Machine(id="A", work_center_id="WC", calendar_id="cal")
    problem = _problem(
        Job(id="j", operations=(_op("a", "A"),)),
        machines=(machine,),
        calendars=[Calendar(id="cal", intervals=((0.0, 100.0),))],
    )

    with pytest.raises(ValueError, match="calendar"):
        semi_active(problem, {"A": ["a"]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operator_needs", [{"pool_id": "p", "quantity": 1, "phase": "run"}]),
        ("batch_family", "f"),
        ("predecessors", [{"operation_id": "x", "max_lag": 5.0}]),
        ("predecessors", [{"operation_id": "x", "overlap_quantity": 1.0}]),
    ],
)
def test_semi_active_refuses_unsupported_operation_features(field: str, value: object) -> None:
    unsupported = Operation.model_validate({**_op("a", "A").model_dump(), field: value})
    job = Job(id="j", operations=(_op("x", "A"), unsupported))

    with pytest.raises(ValueError, match="oracle does not support"):
        semi_active(_problem(job), {"A": ["x", "a"]})


# --- brute_force edges -------------------------------------------------------------------------


def test_brute_force_picks_the_cheaper_option_machine() -> None:
    op = Operation(
        id="a",
        work_center_id="WC",
        quantity=1.0,
        options=(_option("A", 9.0), _option("B", 4.0)),
    )

    value, schedule = brute_force(_problem(Job(id="j", operations=(op,))), K.MAKESPAN)

    assert value == 4.0
    assert schedule.assignments[0].machine_id == "B"


def test_brute_force_maximizes_robustness() -> None:
    jobs = (
        Job(id="early", weight_class=4, due=10.0, operations=(_op("a", "A", seconds=2.0),)),
        Job(id="late", due=100.0, operations=(_op("b", "A", seconds=2.0),)),
    )
    problem = _problem(*jobs)
    compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
    robust = [
        evaluate(compiled, semi_active(problem, {"A": order, "B": []}))[K.ROBUSTNESS]
        for order in (["a", "b"], ["b", "a"])
    ]

    value, _ = brute_force(problem, K.ROBUSTNESS)

    assert value == max(robust)
    assert robust[0] != robust[1]


def test_brute_force_refuses_more_than_a_million_combinations() -> None:
    ops = tuple(_op(f"o{i}", "A") for i in range(10))  # 10! = 3,628,800 sequences
    problem = _problem(Job(id="j", operations=ops))

    assert combination_count(problem) > MAX_COMBINATIONS
    with pytest.raises(ValueError, match="combinations"):
        brute_force(problem, K.MAKESPAN)


def test_brute_force_skips_deadlocked_sequences() -> None:
    job = Job(
        id="j",
        operations=(_op("a", "A"), _op("b", "A", predecessors=[Precedence(operation_id="a")])),
    )

    value, schedule = brute_force(_problem(job), K.MAKESPAN)

    assert value == 10.0
    assert [a.operation_id for a in schedule.assignments] == ["a", "b"]
