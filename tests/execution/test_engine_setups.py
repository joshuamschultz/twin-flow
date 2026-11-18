"""Setups, detached setup, restart warm-up and machine state (T-175, REQ-128, REQ-130, COMP-135).

Setup before an op = `SetupTable.seconds(machine, actual predecessor, op)`, the machine's
`MachineState` when the op is first (D-064), plus the TF-003 D-074 restart warm-up when the
machine's actual idle exceeds the rule's threshold. Idle is measured from the previous
occupation's end, or from `max(now, available_from)` for the first op.

Restart cases use hand-built schedules and are not passed to `verify`: the verifier on this
branch has no warm-up (SDD DEV-8), so a correct restart plan would fail its setup check.
"""

from __future__ import annotations

import pytest
from _engine_cases import (
    SLOW,
    after,
    assert_replays,
    assert_times,
    assert_verified,
    at,
    by_op,
    compile_problem,
    job,
    op,
    plan,
    problem,
    run_plan,
    slow,
)

from twinflow.scheduling.model import (
    AbsDiffTerm,
    CompiledProblem,
    FamilyMatrixEntry,
    Machine,
    MachineState,
    MismatchTerm,
    RestartRule,
    Schedule,
    SetupFunction,
)
from twinflow.scheduling.setups import SetupTable

ABS = SetupFunction(id="ABS", terms=(AbsDiffTerm(param="w", seconds_per_unit=2.0),))
MISMATCH = SetupFunction(id="MIS", terms=(MismatchTerm(param="color", seconds=9.0),))
FAMILY = SetupFunction(
    id="FAM",
    terms=(),
    family_matrix=(
        FamilyMatrixEntry(from_family="F1", to_family="F1", seconds=0.0),
        FamilyMatrixEntry(from_family="F2", to_family="F1", seconds=20.0),
        FamilyMatrixEntry(from_family="F3", to_family="F1", seconds=11.0),
        FamilyMatrixEntry(from_family="F1", to_family="F2", seconds=15.0),
    ),
    default_seconds=30.0,
)
DEFAULT_8 = SetupFunction(id="DEF8", terms=(), default_seconds=8.0)
SETUP_10 = SetupFunction(id="S10", terms=(), default_seconds=10.0)
RESTART = RestartRule(idle_threshold_seconds=20.0, warmup_seconds=5.0)

FREE = Machine(id="M1", work_center_id="A")


def _setups_of(compiled: CompiledProblem, sequence: list[tuple[str | None, str]]) -> list[float]:
    """SetupTable values for (predecessor, op) pairs on M1, from the compiled problem."""
    table = SetupTable.for_problem(compiled.problem)
    return [table.seconds("M1", prev, nxt) for prev, nxt in sequence]


# --- setup functions on the actual predecessor -------------------------------------------------


def _abs_diff_case() -> tuple[CompiledProblem, Schedule]:
    machine = Machine(
        id="M1",
        work_center_id="A",
        setup_function_id="ABS",
        state=MachineState(last_setup_params={"w": 10.0}),
    )
    jobs = [
        job("Ja", op("a", "M1", 10, quantity=SLOW, setup_params={"w": 13.0})),
        job("Jb", op("b", "M1", 10, setup_params={"w": 7.0})),
        job("Jc", op("c", "M1", 10, setup_params={"w": 10.0})),
    ]
    compiled = compile_problem(problem(jobs, (machine,), setup_functions=(ABS,)))
    # a: |13 - 10| * 2 = 6 (from machine state); b: |7 - 13| * 2 = 12; c: |10 - 7| * 2 = 6.
    schedule = plan(at("a", "M1", 0, 6, 16), at("b", "M1", 16, 28, 38), at("c", "M1", 38, 44, 54))
    return compiled, schedule


def test_abs_diff_setup_is_measured_from_machine_state_then_actual_predecessor() -> None:
    compiled, schedule = _abs_diff_case()
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)
    records = by_op(run)

    assert_replays(run, schedule)
    expected = _setups_of(compiled, [(None, "a"), ("a", "b"), ("b", "c")])
    assert expected == [6.0, 12.0, 6.0]
    assert [records[o].setup_seconds for o in "abc"] == pytest.approx(expected)


def test_slip_that_reorders_nothing_keeps_the_planned_setups() -> None:
    compiled, schedule = _abs_diff_case()

    records = by_op(run_plan(compiled, schedule, slow(1.5)))

    # a runs 15 s instead of 10; b and c shift by 5 s with unchanged setups.
    assert_times(records["a"], 0, 6, 21)
    assert_times(records["b"], 21, 33, 43)
    assert_times(records["c"], 43, 49, 59)
    assert [records[o].setup_seconds for o in "abc"] == pytest.approx([6.0, 12.0, 6.0])


def test_mismatch_setup_on_actual_predecessor() -> None:
    machine = Machine(
        id="M1",
        work_center_id="A",
        setup_function_id="MIS",
        state=MachineState(last_setup_params={"color": "red"}),
    )
    colors = {"a": "red", "b": "blue", "c": "blue", "d": "red"}
    jobs = [job(f"J{o}", op(o, "M1", 5, setup_params={"color": c})) for o, c in colors.items()]
    compiled = compile_problem(problem(jobs, (machine,), setup_functions=(MISMATCH,)))
    schedule = plan(
        at("a", "M1", 0, 0, 5),
        at("b", "M1", 5, 14, 19),
        at("c", "M1", 19, 19, 24),
        at("d", "M1", 24, 33, 38),
    )
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)
    records = by_op(run)

    assert_replays(run, schedule)
    expected = _setups_of(compiled, [(None, "a"), ("a", "b"), ("b", "c"), ("c", "d")])
    assert expected == [0.0, 9.0, 0.0, 9.0]
    assert [records[o].setup_seconds for o in "abcd"] == pytest.approx(expected)


def test_former_guard_changeover_matrix() -> None:
    """Former guard "no changeover": a family matrix with a default for a missing pair (T-175)."""
    machine = Machine(
        id="M1",
        work_center_id="A",
        setup_function_id="FAM",
        state=MachineState(last_setup_family="F2"),
    )
    jobs = [
        job("Ja", op("a", "M1", 10, setup_family="F1")),
        job("Jb", op("b", "M1", 10, setup_family="F3")),
        job("Jc", op("c", "M1", 10, setup_family="F1")),
    ]
    compiled = compile_problem(problem(jobs, (machine,), setup_functions=(FAMILY,)))
    # F2->F1 20 (machine state), F1->F3 missing so default 30, F3->F1 11.
    schedule = plan(at("a", "M1", 0, 20, 30), at("b", "M1", 30, 60, 70), at("c", "M1", 70, 81, 91))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)
    records = by_op(run)

    assert_replays(run, schedule)
    expected = _setups_of(compiled, [(None, "a"), ("a", "b"), ("b", "c")])
    assert expected == [20.0, 30.0, 11.0]
    assert [records[o].setup_seconds for o in "abc"] == pytest.approx(expected)


def test_former_guard_non_zero_setup() -> None:
    """Former guard "default_seconds != 0": a work-center setup of 8 s before every op (T-175)."""
    jobs = [job("Ja", op("a", "M1", 10)), job("Jb", op("b", "M1", 10))]
    compiled = compile_problem(
        problem(jobs, (FREE,), setup_functions=(DEFAULT_8,), center_setups={"A": "DEF8"})
    )
    schedule = plan(at("a", "M1", 0, 8, 18), at("b", "M1", 18, 26, 36))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)

    assert_replays(run, schedule)
    assert [by_op(run)[o].setup_seconds for o in "ab"] == pytest.approx([8.0, 8.0])


# --- restart warm-up (TF-003 D-074) -------------------------------------------------------------


def _restart_machine(**fields: object) -> Machine:
    return Machine.model_validate({"id": "M1", "work_center_id": "A", "restart": RESTART, **fields})


def test_restart_warmup_added_only_when_actual_idle_exceeds_the_threshold() -> None:
    # Idle before a: 30 (from now) -> warm-up; b: 10 -> none; c: exactly 20 -> none
    # (must exceed); d: 25 -> warm-up.
    jobs = [job(f"J{o}", op(o, "M1", 10)) for o in "abcd"]
    compiled = compile_problem(problem(jobs, (_restart_machine(),)))
    schedule = plan(
        at("a", "M1", 30, 35, 45),
        at("b", "M1", 55, 55, 65),
        at("c", "M1", 85, 85, 95),
        at("d", "M1", 120, 125, 135),
    )

    run = run_plan(compiled, schedule)
    records = by_op(run)

    assert_replays(run, schedule)
    assert [records[o].setup_seconds for o in "abcd"] == pytest.approx([5.0, 0.0, 0.0, 5.0])


def test_restart_warmup_adds_to_the_setup_function_value() -> None:
    machine = _restart_machine(setup_function_id="DEF8")
    compiled = compile_problem(
        problem([job("J", op("a", "M1", 10))], (machine,), setup_functions=(DEFAULT_8,))
    )
    schedule = plan(at("a", "M1", 30, 43, 53))  # 8 s setup + 5 s warm-up

    record = by_op(run_plan(compiled, schedule))["a"]

    assert_times(record, 30, 43, 53)
    assert record.setup_seconds == pytest.approx(13.0)


def test_restart_idle_of_the_first_op_is_measured_from_available_from() -> None:
    # Idle = 30 - max(now 0, available_from 25) = 5: no warm-up.
    machine = _restart_machine(state=MachineState(available_from=25.0))
    compiled = compile_problem(problem([job("J", op("a", "M1", 10))], (machine,)))
    schedule = plan(at("a", "M1", 30, 30, 40))

    record = by_op(run_plan(compiled, schedule))["a"]

    assert_times(record, 30, 30, 40)
    assert record.setup_seconds == pytest.approx(0.0)


def test_restart_warmup_dropped_when_a_slip_removes_the_planned_idle() -> None:
    # Planned idle before b is 25 (warm-up planned). a runs 40 s instead of 10, so b starts
    # right after it with no idle and no warm-up.
    jobs = [job("Ja", op("a", "M1", 10, quantity=SLOW)), job("Jb", op("b", "M1", 10))]
    compiled = compile_problem(problem(jobs, (_restart_machine(),)))
    schedule = plan(at("a", "M1", 0, 0, 10), at("b", "M1", 35, 40, 50))

    records = by_op(run_plan(compiled, schedule, slow(4.0)))

    assert_times(records["a"], 0, 0, 40)
    assert_times(records["b"], 40, 40, 50)
    assert records["b"].setup_seconds == pytest.approx(0.0)


def test_restart_warmup_added_when_a_slip_creates_idle() -> None:
    # y follows x on M2 after 10 s planned idle. Its job predecessor p (on M1) ends at 45
    # instead of 20, so M2 idles 35 s > 20 and y gets the warm-up.
    m2 = Machine(id="M2", work_center_id="A", restart=RESTART)
    jobs = [
        job("Jx", op("x", "M2", 10)),
        job("Jy", op("p", "M1", 20, quantity=SLOW), op("y", "M2", 10, predecessors=after("p"))),
    ]
    compiled = compile_problem(problem(jobs, (FREE, m2)))
    schedule = plan(at("x", "M2", 0, 0, 10), at("p", "M1", 0, 0, 20), at("y", "M2", 20, 20, 30))
    assert_verified(compiled, schedule)  # no warm-up is planned anywhere, so verify applies

    records = by_op(run_plan(compiled, schedule, slow(2.25)))

    assert_times(records["p"], 0, 0, 45)
    assert_times(records["y"], 45, 50, 60)
    assert records["y"].setup_seconds == pytest.approx(5.0)


# --- detached setup ----------------------------------------------------------------------------


def _detached_case(*, detached: bool) -> tuple[CompiledProblem, Schedule]:
    m2 = Machine(id="M2", work_center_id="A", setup_function_id="S10")
    jobs = [
        job(
            "J",
            op("p", "M1", 30, quantity=SLOW),
            op("d", "M2", 10, setup_detached=detached, predecessors=after("p")),
        )
    ]
    compiled = compile_problem(problem(jobs, (FREE, m2), setup_functions=(SETUP_10,)))
    d = at("d", "M2", 20, 30, 40) if detached else at("d", "M2", 30, 40, 50)
    return compiled, plan(at("p", "M1", 0, 0, 30), d)


@pytest.mark.parametrize("detached", [True, False])
def test_detached_and_attached_setup_replay_their_verified_plans(detached: bool) -> None:
    compiled, schedule = _detached_case(detached=detached)
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_detached_setup_finishes_before_material_and_the_run_waits_for_the_gate() -> None:
    # p ends at 45 instead of 30. d's setup still runs [20, 30); its run waits for 45.
    compiled, schedule = _detached_case(detached=True)

    record = by_op(run_plan(compiled, schedule, slow(1.5)))["d"]

    assert_times(record, 20, 30, 55)
    assert record.setup_seconds == pytest.approx(10.0)
    assert record.run_seconds == pytest.approx(10.0)
    assert record.gate_time == pytest.approx(45.0)


def test_attached_setup_waits_for_the_gate_before_setup() -> None:
    compiled, schedule = _detached_case(detached=False)

    record = by_op(run_plan(compiled, schedule, slow(1.5)))["d"]

    assert_times(record, 45, 55, 65)
