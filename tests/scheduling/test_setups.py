"""Setup oracle (T-088, COMP-002, REQ-064, D-064).

Direct computation (`setup_seconds`) is checked term by term, then `SetupTable` is
checked against it over every ordered pair of a seeded 30-operation problem, with a
call counter proving each pair is computed once.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from twinflow.scheduling import setups
from twinflow.scheduling.model import (
    AbsDiffTerm,
    FamilyMatrixEntry,
    Job,
    Machine,
    MachineState,
    MismatchTerm,
    Operation,
    Problem,
    ProcessingOption,
    SetupFunction,
    SetupParams,
    WorkCenter,
)
from twinflow.scheduling.setups import SetupTable, setup_seconds

GAUGE = AbsDiffTerm(param="gauge", seconds_per_unit=10.0)
TOOL = MismatchTerm(param="tool", seconds=300.0)


def op(
    op_id: str,
    params: SetupParams | None = None,
    family: str | None = None,
    machine_id: str = "m1",
    work_center_id: str = "wc1",
) -> Operation:
    return Operation(
        id=op_id,
        work_center_id=work_center_id,
        quantity=1.0,
        options=(
            ProcessingOption(
                machine_id=machine_id, fixed_seconds=0, unit_seconds=1, preference_cost=0
            ),
        ),
        setup_params=params or {},
        setup_family=family,
    )


def function(**kwargs: Any) -> SetupFunction:
    return SetupFunction(id="f", **kwargs)


# --- direct computation -------------------------------------------------------------------


def test_abs_diff_is_rate_times_absolute_difference() -> None:
    fn = function(terms=(GAUGE,))
    assert setup_seconds(fn, op("a", {"gauge": 2.5}), op("b", {"gauge": 1.0})) == 15.0
    assert setup_seconds(fn, op("a", {"gauge": 1.0}), op("b", {"gauge": 2.5})) == 15.0


def test_mismatch_gives_seconds_when_values_differ_and_zero_when_equal() -> None:
    fn = function(terms=(TOOL,))
    assert setup_seconds(fn, op("a", {"tool": "t1"}), op("b", {"tool": "t2"})) == 300.0
    assert setup_seconds(fn, op("a", {"tool": "t1"}), op("b", {"tool": "t1"})) == 0.0


def test_sum_combines_sequential_changeovers_and_max_parallel_ones() -> None:
    prev = op("a", {"gauge": 0.0, "tool": "t1"})
    nxt = op("b", {"gauge": 20.0, "tool": "t2"})
    assert setup_seconds(function(terms=(GAUGE, TOOL)), prev, nxt) == 500.0
    assert setup_seconds(function(terms=(GAUGE, TOOL), combine="max"), prev, nxt) == 300.0


def test_missing_param_falls_back_to_family_matrix() -> None:
    fn = function(
        terms=(GAUGE, TOOL),
        family_matrix=(FamilyMatrixEntry(from_family="x", to_family="y", seconds=42.0),),
        default_seconds=7.0,
    )
    prev = op("a", {"gauge": 1.0, "tool": "t1"}, family="x")
    nxt = op("b", {"gauge": 2.0}, family="y")
    assert setup_seconds(fn, prev, nxt) == 42.0


def test_missing_param_and_no_matrix_entry_falls_back_to_default() -> None:
    fn = function(
        terms=(GAUGE,),
        family_matrix=(FamilyMatrixEntry(from_family="x", to_family="y", seconds=42.0),),
        default_seconds=7.0,
    )
    assert setup_seconds(fn, op("a", {}, family="y"), op("b", {"gauge": 1.0}, family="x")) == 7.0
    assert setup_seconds(fn, op("a", {}), op("b", {"gauge": 1.0})) == 7.0


def test_same_family_without_matrix_entry_uses_default() -> None:
    fn = function(terms=(GAUGE,), default_seconds=7.0)
    assert setup_seconds(fn, op("a", family="x"), op("b", family="x")) == 7.0


def test_abs_diff_on_string_param_counts_as_missing() -> None:
    fn = function(terms=(GAUGE,), default_seconds=7.0)
    assert setup_seconds(fn, op("a", {"gauge": "thick"}), op("b", {"gauge": 1.0})) == 7.0


def test_function_without_terms_is_decided_by_family_matrix() -> None:
    fn = function(
        terms=(),
        family_matrix=(FamilyMatrixEntry(from_family="x", to_family="y", seconds=42.0),),
        default_seconds=7.0,
    )
    assert setup_seconds(fn, op("a", family="x"), op("b", family="y")) == 42.0
    assert setup_seconds(fn, op("a", family="y"), op("b", family="x")) == 7.0


def test_first_operation_uses_machine_state_params() -> None:
    fn = function(terms=(GAUGE,))
    state = MachineState(last_setup_params={"gauge": 4.0})
    assert setup_seconds(fn, state, op("b", {"gauge": 1.0})) == 30.0


def test_first_operation_uses_machine_state_family() -> None:
    fn = function(
        terms=(GAUGE,),
        family_matrix=(FamilyMatrixEntry(from_family="start", to_family="y", seconds=9.0),),
    )
    state = MachineState(last_setup_family="start")
    assert setup_seconds(fn, state, op("b", family="y")) == 9.0


def test_no_function_means_zero() -> None:
    assert setup_seconds(None, op("a", {"gauge": 0.0}), op("b", {"gauge": 9.0})) == 0.0


# --- SetupTable ---------------------------------------------------------------------------


def two_function_problem(machine_function: str | None, center_function: str | None) -> Problem:
    return Problem(
        id="p",
        jobs=(
            Job(
                id="j",
                operations=(
                    op("a", {"gauge": 0.0, "tool": "t1"}),
                    op("b", {"gauge": 1.0, "tool": "t2"}),
                ),
            ),
        ),
        machines=(
            Machine(
                id="m1",
                work_center_id="wc1",
                setup_function_id=machine_function,
                state=MachineState(last_setup_params={"gauge": 3.0, "tool": "t2"}),
            ),
        ),
        work_centers=(WorkCenter(id="wc1", setup_function_id=center_function),),
        setup_functions=(
            SetupFunction(id="gauge_fn", terms=(GAUGE,)),
            SetupFunction(id="tool_fn", terms=(TOOL,)),
        ),
    )


def test_machine_function_overrides_work_center_function() -> None:
    table = SetupTable.for_problem(two_function_problem("gauge_fn", "tool_fn"))
    assert table.seconds("m1", "a", "b") == 10.0


def test_work_center_function_applies_when_machine_has_none() -> None:
    table = SetupTable.for_problem(two_function_problem(None, "tool_fn"))
    assert table.seconds("m1", "a", "b") == 300.0


def test_table_with_no_function_gives_zero() -> None:
    table = SetupTable.for_problem(two_function_problem(None, None))
    assert table.seconds("m1", "a", "b") == 0.0
    assert table.seconds("m1", None, "b") == 0.0


def test_table_uses_machine_state_when_previous_is_none() -> None:
    table = SetupTable.for_problem(two_function_problem("gauge_fn", None))
    assert table.seconds("m1", None, "a") == 30.0


def random_problem(seed: int, operation_count: int = 30) -> Problem:
    rng = np.random.default_rng(seed)
    families = ("f0", "f1", "f2")
    matrix = tuple(
        FamilyMatrixEntry(from_family=a, to_family=b, seconds=float(rng.integers(0, 50)))
        for a in families
        for b in families
        if rng.random() < 0.7
    )
    operations = []
    for index in range(operation_count):
        params: SetupParams = {}
        if rng.random() < 0.8:
            params["gauge"] = float(rng.integers(0, 10))
        if rng.random() < 0.8:
            params["tool"] = str(rng.choice(["t1", "t2", "t3"]))
        family = str(rng.choice(families)) if rng.random() < 0.9 else None
        operations.append(op(f"o{index:02d}", params, family))
    return Problem(
        id="random",
        jobs=(Job(id="j", operations=tuple(operations)),),
        machines=(
            Machine(
                id="m1",
                work_center_id="wc1",
                state=MachineState(last_setup_params={"gauge": 5.0}, last_setup_family="f1"),
            ),
            Machine(id="m2", work_center_id="wc1", setup_function_id="max_fn"),
            Machine(id="m3", work_center_id="wc2"),
        ),
        work_centers=(
            WorkCenter(id="wc1", setup_function_id="sum_fn"),
            WorkCenter(id="wc2"),
        ),
        setup_functions=(
            SetupFunction(
                id="sum_fn", terms=(GAUGE, TOOL), family_matrix=matrix, default_seconds=11.0
            ),
            SetupFunction(
                id="max_fn",
                terms=(GAUGE, TOOL),
                combine="max",
                family_matrix=matrix,
                default_seconds=13.0,
            ),
        ),
    )


def test_table_equals_direct_computation_and_computes_each_pair_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = random_problem(seed=7)
    operations = problem.jobs[0].operations
    expected_function = {"m1": "sum_fn", "m2": "max_fn", "m3": None}
    functions = {fn.id: fn for fn in problem.setup_functions}
    machines = {machine.id: machine for machine in problem.machines}

    calls: list[tuple[str, str]] = []
    direct = setups.setup_seconds

    def counting(fn: SetupFunction | None, prev: Operation | MachineState, nxt: Operation) -> float:
        calls.append((prev.id if isinstance(prev, Operation) else "<state>", nxt.id))
        return direct(fn, prev, nxt)

    monkeypatch.setattr(setups, "setup_seconds", counting)
    table = SetupTable.for_problem(problem)
    prevs: list[Operation | None] = [None, *operations]

    for _ in range(2):
        for machine_id, function_id in expected_function.items():
            fn = functions[function_id] if function_id else None
            for prev in prevs:
                for nxt in operations:
                    prev_side = prev if prev is not None else machines[machine_id].state
                    want = direct(fn, prev_side, nxt)
                    got = table.seconds(machine_id, prev.id if prev else None, nxt.id)
                    assert got == want
                    assert got >= 0.0

    assert len(calls) == len(expected_function) * len(prevs) * len(operations)
