"""Tests for the brute-force oracle and the two Pinedo fixtures (T-090, COMP-012).

Example 7.1.1 (p.182, J4 || Cmax) has optimum makespan 28 (Example 7.1.4, p.189).
Example 14.2.1 (p.376-377, 1 | s_jk | sum w_j T_j) has optimum 98 at 2, 4, 3, 1.
Requirements: REQ-078, REQ-079.
"""

from __future__ import annotations

import ast
import itertools
import math
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from generators import random_request
from oracle import (
    MAX_COMBINATIONS,
    SequenceDeadlock,
    brute_force,
    combination_count,
    semi_active,
)
from oracle_math import SetupCalculator, objective_values

from twinflow.scheduling import solve
from twinflow.scheduling.model import (
    AbsDiffTerm,
    Assignment,
    Calendar,
    CompiledProblem,
    FamilyMatrixEntry,
    FrozenZone,
    Job,
    Machine,
    MachineState,
    MismatchTerm,
    ObjectiveKind,
    Operation,
    Precedence,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    ProcessingOption,
    Schedule,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    SolveResult,
    WorkCenter,
)
from twinflow.scheduling.objectives import evaluate
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.setups import SetupTable

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
ORACLE_MODULES = ("oracle.py", "oracle_math.py")
FORBIDDEN_IMPORTS = (
    "twinflow.scheduling.setups",
    "twinflow.scheduling.objectives",
    "twinflow.scheduling.verifier",
)
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


# --- independence from the code under test (TF-002 review item 4) ------------------------------


def _imported_modules(path: Path) -> set[str]:
    """Every module a file imports, including `from x import y` as both `x` and `x.y`."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("name", ORACLE_MODULES)
def test_oracle_module_imports_no_setups_objectives_or_verifier(name: str) -> None:
    imported = _imported_modules(HERE / name)

    leaks = sorted(
        module
        for module in imported
        for forbidden in FORBIDDEN_IMPORTS
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert leaks == []


# --- the oracle's own setup arithmetic, hand-computed from D-064 and DEV-4 --------------------

_MATRIX = (
    FamilyMatrixEntry(from_family="F", to_family="G", seconds=11.0),
    FamilyMatrixEntry(from_family="G", to_family="F", seconds=13.0),
)
_SUM = SetupFunction(
    id="SUM",
    terms=(
        AbsDiffTerm(param="w", seconds_per_unit=2.0),
        MismatchTerm(param="c", seconds=3.0),
    ),
    family_matrix=_MATRIX,
    default_seconds=17.0,
)
_MAX = _SUM.model_copy(update={"id": "MAX", "combine": "max"})
_MATRIX_ONLY = SetupFunction(id="MATRIX", terms=(), family_matrix=_MATRIX, default_seconds=19.0)


def _setup_problem(
    *ops: Operation, machine_function: str | None = "SUM", state: MachineState | None = None
) -> Problem:
    machines = (
        Machine(
            id="A",
            work_center_id="WC",
            setup_function_id=machine_function,
            state=state or MachineState(),
        ),
        Machine(id="B", work_center_id="WC"),
    )
    return _problem(
        Job(id="j", operations=ops),
        machines=machines,
        work_centers=(WorkCenter(id="WC", setup_function_id="MAX"),),
        setup_functions=(_SUM, _MAX, _MATRIX_ONLY),
    )


def _setup_op(op_id: str, params: dict[str, float | str], family: str | None = None) -> Operation:
    return _op(op_id, "A", "B", setup_params=params, setup_family=family)


@pytest.mark.parametrize(
    ("prev", "nxt", "expected"),
    [
        ({"w": 3.0, "c": "x"}, {"w": 7.0, "c": "y"}, 2.0 * 4.0 + 3.0),  # terms summed
        ({"w": 7.0, "c": "x"}, {"w": 3.0, "c": "x"}, 8.0),  # |b - a|, mismatch 0
        ({"w": 3.0}, {"w": 7.0, "c": "y"}, 11.0),  # prev lacks c: family matrix F -> G
        ({"w": "wide", "c": "x"}, {"w": 7.0, "c": "x"}, 11.0),  # DEV-4: text on abs_diff
        ({"w": 3.0, "c": "1"}, {"w": 3.0, "c": 1.0}, 3.0),  # text vs number differ
    ],
)
def test_oracle_setup_resolves_terms_then_family_matrix(
    prev: dict[str, float | str], nxt: dict[str, float | str], expected: float
) -> None:
    problem = _setup_problem(_setup_op("p", prev, "F"), _setup_op("n", nxt, "G"))

    assert SetupCalculator(problem).seconds("A", "p", "n") == expected


def test_oracle_setup_combine_max_takes_the_largest_term() -> None:
    ops = (_setup_op("p", {"w": 3.0, "c": "x"}), _setup_op("n", {"w": 4.5, "c": "y"}))
    problem = _setup_problem(*ops, machine_function="MAX")

    assert SetupCalculator(problem).seconds("A", "p", "n") == 3.0  # max(2 * 1.5, 3)


def test_oracle_setup_same_family_without_entry_gives_default_not_zero() -> None:
    ops = (_setup_op("p", {}, "F"), _setup_op("n", {}, "F"))

    assert SetupCalculator(_setup_problem(*ops)).seconds("A", "p", "n") == 17.0


def test_oracle_setup_no_family_and_missing_param_gives_default() -> None:
    ops = (_setup_op("p", {"w": 1.0}), _setup_op("n", {"c": "x"}, "G"))

    assert SetupCalculator(_setup_problem(*ops)).seconds("A", "p", "n") == 17.0


def test_oracle_setup_function_without_terms_is_a_family_matrix() -> None:
    same_params = {"w": 1.0, "c": "x"}
    ops = (_setup_op("p", same_params, "G"), _setup_op("n", same_params, "F"))
    calculator = SetupCalculator(_setup_problem(*ops, machine_function="MATRIX"))

    assert calculator.seconds("A", "p", "n") == 13.0
    assert calculator.seconds("A", "n", "p") == 11.0


def test_oracle_setup_falls_back_to_work_center_function_then_to_zero() -> None:
    ops = (_setup_op("p", {"w": 3.0, "c": "x"}), _setup_op("n", {"w": 4.5, "c": "y"}))
    calculator = SetupCalculator(_setup_problem(*ops, machine_function=None))

    assert calculator.seconds("A", "p", "n") == 3.0  # work center WC uses MAX
    no_center_function = _setup_problem(*ops).model_copy(
        update={"work_centers": (WorkCenter(id="WC"),)}
    )
    assert SetupCalculator(no_center_function).seconds("B", "p", "n") == 0.0


def test_oracle_setup_first_operation_reads_the_machine_state() -> None:
    state = MachineState(last_setup_params={"w": 10.0}, last_setup_family="G")
    calculator = SetupCalculator(
        _setup_problem(_setup_op("n", {"w": 4.0, "c": "x"}, "F"), state=state)
    )

    assert calculator.seconds("A", None, "n") == 13.0  # state lacks c: matrix G -> F


def test_oracle_setup_first_operation_uses_state_params_when_complete() -> None:
    state = MachineState(last_setup_params={"w": 10.0, "c": "x"})
    calculator = SetupCalculator(_setup_problem(_setup_op("n", {"w": 4.0, "c": "y"}), state=state))

    assert calculator.seconds("A", None, "n") == 2.0 * 6.0 + 3.0


# --- the oracle's own objective arithmetic, hand-computed from the SDD formulas ----------------


def _priced_option(machine_id: str, preference_cost: float) -> ProcessingOption:
    return ProcessingOption(
        machine_id=machine_id, fixed_seconds=1.0, unit_seconds=0.0, preference_cost=preference_cost
    )


def _objective_case(tolerance: float | None) -> CompiledProblem:
    """J1 (class 2, due 30), J2 (override 3, due 100), J3 (no due), J4 held; now = 10."""
    a = _op("a", "A").model_copy(
        update={"options": (_priced_option("A", 1.5), _priced_option("B", 0.0))}
    )
    jobs = (
        Job(id="J1", weight_class=2, due=30.0, operations=(a, _op("b", "B"))),
        Job(id="J2", weight_override=3.0, due=100.0, operations=(_op("c", "A", "B"),)),
        Job(id="J3", operations=(_op("d", "B"),)),
        Job(id="J4", due=0.0, operations=(_op("e", "B"),)),
    )
    machines = (
        Machine(id="A", work_center_id="WC", hour_cost=36.0, setup_hour_cost=72.0),
        Machine(id="B", work_center_id="WC"),
    )
    frozen = FrozenZone(until=1000.0, tolerance=tolerance) if tolerance is not None else None
    return CompiledProblem(
        problem=_problem(*jobs, machines=machines, now=10.0),
        profile=SchedulingProfile(frozen_zone=frozen),
        held_job_ids=("J4",),
    )


def _assign(op_id: str, machine_id: str, start: float, setup_end: float, end: float) -> Assignment:
    return Assignment(
        operation_id=op_id, machine_id=machine_id, start=start, setup_end=setup_end, end=end
    )


_CASE_SCHEDULE = Schedule(
    status="FEASIBLE",
    assignments=(
        _assign("a", "A", 10.0, 14.0, 20.0),
        _assign("c", "A", 20.0, 20.0, 50.0),
        _assign("b", "B", 20.0, 22.0, 40.0),
        _assign("d", "B", 40.0, 40.0, 45.0),
        _assign("e", "B", 45.0, 45.0, 60.0),  # held job: ignored everywhere
        _assign("zz", "B", 60.0, 61.0, 99.0),  # not in the problem: ignored everywhere
    ),
    solver="hand",
    elapsed_seconds=0.0,
)
_CASE_PREVIOUS = PreviousPlan(
    plan_id="prev",
    problem_digest="d",
    assignments=(
        PreviousAssignment(operation_id="a", machine_id="A", start=10.0),  # same
        PreviousAssignment(operation_id="c", machine_id="B", start=20.0),  # machine changed
        PreviousAssignment(operation_id="b", machine_id="B", start=25.0),  # moved 5
        PreviousAssignment(operation_id="d", machine_id="B", start=30.0),  # moved 10
        PreviousAssignment(operation_id="e", machine_id="A", start=0.0),  # held
        PreviousAssignment(operation_id="gone", machine_id="A", start=0.0),  # not scheduled
    ),
)


def test_oracle_objectives_match_hand_computed_sdd_formulas() -> None:
    # C1 = 40, C2 = 50, C3 = 45; w1 = 2 (class 2), w2 = 3 (override), w3 = 1.
    values = objective_values(_objective_case(5.0), _CASE_SCHEDULE, _CASE_PREVIOUS)

    assert values == {
        K.MAKESPAN: 40.0,
        K.WEIGHTED_COMPLETION: 2 * 40.0 + 3 * 50.0 + 1 * 45.0,
        K.MAX_LATENESS: 10.0,
        K.TARDY_COUNT: 1.0,
        K.WEIGHTED_TARDY_COUNT: 2.0,
        K.TOTAL_TARDINESS: 10.0,
        K.WEIGHTED_TARDINESS: 20.0,
        K.EARLINESS_TARDINESS: 10.0 + 50.0,
        K.TOTAL_SETUP: 4.0 + 2.0,
        K.COST: (6.0 * 36.0 + 4.0 * 72.0 + 30.0 * 36.0) / 3600.0,
        K.CHURN: 2.0,  # c changed machine, d moved 10 > 5; b moved exactly 5
        K.ROBUSTNESS: (2 * (30.0 - 40.0) + 3 * (100.0 - 50.0)) / (2 * 20.0 + 3 * 90.0),
        K.SOFT_PENALTY: 1.5,
    }


def test_oracle_churn_tolerance_is_zero_without_frozen_zone_and_zero_without_plan() -> None:
    compiled = _objective_case(None)

    assert objective_values(compiled, _CASE_SCHEDULE, _CASE_PREVIOUS)[K.CHURN] == 3.0
    assert objective_values(compiled, _CASE_SCHEDULE)[K.CHURN] == 0.0


def test_oracle_robustness_is_zero_when_the_denominator_is_not_positive() -> None:
    job = Job(id="j", due=10.0, operations=(_op("a", "A"),))
    compiled = CompiledProblem(problem=_problem(job, now=10.0), profile=SchedulingProfile())
    schedule = semi_active(compiled.problem, {"A": ["a"], "B": []})

    assert objective_values(compiled, schedule)[K.ROBUSTNESS] == 0.0


def test_oracle_objectives_of_an_empty_schedule_are_zero() -> None:
    empty = Schedule(status="FEASIBLE", assignments=(), solver="hand", elapsed_seconds=0.0)

    assert set(objective_values(_objective_case(5.0), empty).values()) == {0.0}


# --- cross-check against the production modules over generated problems -----------------------

CROSS_CHECK_SEEDS = range(200)
TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class _Solved:
    seed: int
    request: SolveRequest
    compiled: CompiledProblem
    result: SolveResult


def _varied_params(op: Operation, k: int) -> Operation:
    """Every 4th op lacks `c` and every 4th has text `w`, so the matrix and default decide."""
    params = dict(op.setup_params)
    if k % 4 == 1:
        params.pop("c", None)
    elif k % 4 == 2 and "w" in params:
        params["w"] = "wide"
    return op.model_copy(update={"setup_params": params})


def _two_terms(function: SetupFunction) -> SetupFunction:
    """With one term, sum and max agree; a second term makes `combine` observable."""
    extra = AbsDiffTerm(param="w", seconds_per_unit=0.5)
    return function.model_copy(update={"terms": (*function.terms, extra)})


def _cross_check_request(seed: int) -> SolveRequest:
    """`random_request(seed)` with priced machines and every D-064 resolution branch in use.

    The generator leaves hour costs at 0, gives every op both setup parameters as numbers
    and a one-term max function, which would leave cost, the family matrix, the text rule
    and `combine` unchecked.
    """
    problem = random_request(seed).problem
    machines = tuple(
        m.model_copy(update={"hour_cost": 7.0 + seed % 5 + i, "setup_hour_cost": 13.0 + i})
        for i, m in enumerate(problem.machines)
    )
    counter = itertools.count()
    jobs = tuple(
        job.model_copy(
            update={"operations": tuple(_varied_params(op, next(counter)) for op in job.operations)}
        )
        for job in problem.jobs
    )
    functions = tuple(_two_terms(f) for f in problem.setup_functions)
    update = {"machines": machines, "jobs": jobs, "setup_functions": functions}
    request = random_request(seed)
    return request.model_copy(update={"problem": problem.model_copy(update=update)})


@cache
def _verified_baseline_solves() -> tuple[_Solved, ...]:
    """Baseline solves of the cross-check requests whose schedules verified."""
    solved = []
    for seed in CROSS_CHECK_SEEDS:
        request = _cross_check_request(seed)
        result = solve(request)
        if result.verified:
            solved.append(_Solved(seed, request, apply_overrides(request), result))
    return tuple(solved)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=TOLERANCE, abs_tol=TOLERANCE)


def test_cross_check_covers_enough_verified_problems_and_features() -> None:
    solved = _verified_baseline_solves()
    values = [s.result.objective_values for s in solved]

    assert len(solved) >= 100
    assert sum(v[K.TOTAL_SETUP] > 0 for v in values) >= 20
    assert sum(v[K.CHURN] > 0 for v in values) >= 5
    assert sum(v[K.SOFT_PENALTY] > 0 for v in values) >= 5
    assert sum(v[K.COST] > 0 for v in values) >= 20
    assert sum(v[K.ROBUSTNESS] != 0 for v in values) >= 20
    assert sum(v[K.WEIGHTED_TARDINESS] > 0 for v in values) >= 20
    assert sum(bool(s.compiled.held_job_ids) for s in solved) >= 10


def test_oracle_objectives_equal_production_evaluate_on_200_generated_problems() -> None:
    disagreements = []
    for s in _verified_baseline_solves():
        ours = objective_values(s.compiled, s.result.schedule, s.request.previous_plan)
        theirs = s.result.objective_values
        assert set(ours) == set(theirs) == set(ObjectiveKind)
        disagreements += [
            (s.seed, kind.value, ours[kind], theirs[kind])
            for kind in ObjectiveKind
            if not _close(ours[kind], theirs[kind])
        ]

    assert disagreements == []


def _used_setup_triples(schedule: Schedule) -> list[tuple[str, str | None, str]]:
    """(machine, previous op or None, next op) in machine order (start, end, id; DEV-11)."""
    by_machine: dict[str, list[Assignment]] = {}
    for a in schedule.assignments:
        by_machine.setdefault(a.machine_id, []).append(a)
    triples: list[tuple[str, str | None, str]] = []
    for machine_id, assignments in by_machine.items():
        ordered = sorted(assignments, key=lambda a: (a.start, a.end, a.operation_id))
        previous: list[str | None] = [None, *(a.operation_id for a in ordered[:-1])]
        triples += [
            (machine_id, prev, a.operation_id) for prev, a in zip(previous, ordered, strict=True)
        ]
    return triples


def test_oracle_setups_equal_production_setup_table_for_every_used_pair() -> None:
    disagreements = []
    nonzero = 0
    for s in _verified_baseline_solves():
        ours, theirs = (
            SetupCalculator(s.compiled.problem),
            SetupTable.for_problem(s.compiled.problem),
        )
        for triple in _used_setup_triples(s.result.schedule):
            mine, production = ours.seconds(*triple), theirs.seconds(*triple)
            nonzero += mine > 0
            if not _close(mine, production):
                disagreements.append((s.seed, triple, mine, production))

    assert disagreements == []
    assert nonzero >= 50
