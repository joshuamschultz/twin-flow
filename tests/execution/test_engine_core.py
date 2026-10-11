"""Executor core under plan control (T-173, REQ-128, REQ-131, COMP-135, COMP-136, D-088).

Unary machines without calendars, setups or operators. Plans that "equal the plan" are
checked with `scheduling.verify` first; slip expectations are computed by hand.

API this suite pins beyond the `test_fidelity.py` contract (kept minimal):

* The executor gates are enforced even when a hand-built plan's start is earlier than the
  gate: job release, op `ready_time` and machine `available_from` hold an operation back
  (the verifier would reject such a plan; the executor still obeys its rules).
* `PlanControl` raises `ValueError` for a schedule that misses or double-assigns a pending
  operation or uses a machine that is not one of the op's options, either when it is built
  or when `execute` reads it (the tests accept both).
* `OpRecord` violation fields are numbers, 0.0 when nothing was violated
  (`interrupted_seconds`, `overrun_seconds`, `max_lag_violation_seconds`);
  `pin_window_missed` and `censored` are booleans. `gate_time` is when the op's material
  gate opened (max of job release, ready time and each predecessor end plus min lag);
  `machine_free_time` is when its machine became free for it (the previous unit's end, else
  `max(now, available_from)`). `operator_time` and `calendar_time` exist (values are T-176
  and T-174 detail); `slip_cause` is filled by metrics (T-181) and is not tested here.
* `ExecutionRun.downtime_by_machine` is a mapping whose values are 0 without outages.
* A detached op's `setup_end` is when its setup finished, even when its run then waits
  for the material gate (T-175).

Helpers live in `_engine_cases.py`. The engine is imported as `from twinflow.execution
import engine` so import sorting does not change once the module exists. Factors come
from `QuantityFactors`, a `DistributionSource` keyed by lot quantity, because `OpContext`
carries no operation id.
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
    option,
    plan,
    problem,
    run_plan,
    slow,
)

from twinflow.execution import engine
from twinflow.execution.shop import ShopModel, build_shop
from twinflow.execution.sources import DeterministicSource
from twinflow.scheduling.model import (
    Assignment,
    Job,
    Machine,
    MachineState,
    Operation,
    Schedule,
)

M1 = Machine(id="M1", work_center_id="A")
M2 = Machine(id="M2", work_center_id="A")
M3 = Machine(id="M3", work_center_id="B")
MACHINES = (M1, M2, M3)


def _slip_problem() -> tuple[list[Job], Schedule]:
    """o1 (slowed) feeds o2 on M2 and precedes o3 on M1; o4 follows o2 on M2; J4, o7 apart."""
    jobs = [
        job(
            "J1",
            op("o1", "M1", 10, quantity=SLOW),
            op("o2", "M2", 10, predecessors=after("o1")),
        ),
        job("J2", op("o3", "M1", 20)),
        job("J3", op("o4", "M2", 5)),
        job(
            "J4",
            op("o5", "M3", 8, work_center_id="B"),
            op("o6", "M3", 4, work_center_id="B", predecessors=after("o5")),
        ),
        job("J5", op("o7", "M2", 10)),
    ]
    schedule = plan(
        at("o1", "M1", 0, 0, 10),
        at("o2", "M2", 10, 10, 20),
        at("o3", "M1", 10, 10, 30),
        at("o4", "M2", 20, 20, 25),
        at("o5", "M3", 0, 0, 8),
        at("o6", "M3", 8, 8, 12),
        at("o7", "M2", 40, 40, 50),
    )
    return jobs, schedule


# --- machine order and planned start (D-088) ---------------------------------------------------


def test_execute_machine_order_is_start_then_end_then_operation_id() -> None:
    # "z" before "a" by start; the empty "y" [25, 25) before "c" [25, 35) by end, though
    # "c" < "y" by id. Ordering by id or by (start, id) would move z, a or y.
    compiled = compile_problem(
        problem(
            [
                job("Jz", op("z", "M1", 10)),
                job("Ja", op("a", "M1", 10)),
                job("Jy", op("y", "M1", 0)),
                job("Jc", op("c", "M1", 10)),
            ],
            MACHINES,
        )
    )
    schedule = plan(
        at("z", "M1", 0, 0, 10),
        at("a", "M1", 10, 10, 20),
        at("y", "M1", 25, 25, 25),
        at("c", "M1", 25, 25, 35),
    )
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_execute_planned_start_is_the_earliest_start_on_an_idle_machine() -> None:
    compiled = compile_problem(problem([job("J", op("o", "M1", 10))], MACHINES))
    schedule = plan(at("o", "M1", 50, 50, 60))
    assert_verified(compiled, schedule)

    record = by_op(run_plan(compiled, schedule))["o"]

    assert_times(record, 50, 50, 60)


def test_execute_end_to_gate_precedence_replays_and_records_gate_time() -> None:
    compiled = compile_problem(
        problem(
            [job("J", op("o1", "M1", 10), op("o2", "M2", 15, predecessors=after("o1")))],
            MACHINES,
        )
    )
    schedule = plan(at("o1", "M1", 0, 0, 10), at("o2", "M2", 10, 10, 25))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule)

    assert_replays(run, schedule)
    assert by_op(run)["o2"].gate_time == pytest.approx(10.0)


def test_execute_cross_center_substitute_runs_on_its_planned_machine() -> None:
    substitute = Operation(
        id="s",
        work_center_id="A",
        quantity=1.0,
        options=(option("M1", 10), option("M3", 12, work_center_id="B")),
    )
    compiled = compile_problem(
        problem([job("Js", substitute), job("Jt", op("t", "M1", 10))], MACHINES)
    )
    schedule = plan(at("s", "M3", 0, 0, 12), at("t", "M1", 0, 0, 10))
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


# --- gates: release, ready time, available_from -----------------------------------------------


def test_execute_job_release_holds_an_op_planned_before_it() -> None:
    compiled = compile_problem(problem([job("J", op("o", "M1", 10), release=30)], MACHINES))
    early = plan(at("o", "M1", 0, 0, 10))  # unverified on purpose: starts before release

    record = by_op(run_plan(compiled, early))["o"]

    assert_times(record, 30, 30, 40)
    assert record.gate_time == pytest.approx(30.0)


def test_execute_ready_time_holds_an_op_planned_before_it() -> None:
    compiled = compile_problem(problem([job("J", op("o", "M1", 10, ready_time=20.0))], MACHINES))
    early = plan(at("o", "M1", 0, 0, 10))  # unverified on purpose: starts before ready_time

    assert_times(by_op(run_plan(compiled, early))["o"], 20, 20, 30)


def test_execute_ready_time_replays_a_verified_plan() -> None:
    compiled = compile_problem(problem([job("J", op("o", "M1", 10, ready_time=20.0))], MACHINES))
    schedule = plan(at("o", "M1", 20, 20, 30))
    assert_verified(compiled, schedule)

    assert_replays(run_plan(compiled, schedule), schedule)


def test_execute_machine_available_from_holds_its_first_op() -> None:
    busy = Machine(id="M1", work_center_id="A", state=MachineState(available_from=35.0))
    compiled = compile_problem(problem([job("J", op("o", "M1", 10))], (busy, M2, M3)))
    early = plan(at("o", "M1", 0, 0, 10))  # unverified on purpose: before available_from

    record = by_op(run_plan(compiled, early))["o"]

    assert_times(record, 35, 35, 45)
    assert record.machine_free_time == pytest.approx(35.0)


def test_former_guard_plan_release_and_no_dispatch_policy_only() -> None:
    """Former guard "plan release": jobs released later run by their release (T-173)."""
    jobs = [
        job("J1", op("p", "M1", 20)),
        job("J2", op("q", "M1", 10), release=40),
        job("J3", op("r", "M2", 10), release=15),
    ]
    compiled = compile_problem(problem(jobs, MACHINES))
    schedule = plan(at("p", "M1", 0, 0, 20), at("q", "M1", 40, 40, 50), at("r", "M2", 15, 15, 25))
    assert_verified(compiled, schedule)

    replay = run_plan(compiled, schedule)
    by_release = by_op(run_plan(compiled, plan(*(_at_zero(a) for a in schedule.assignments))))

    assert_replays(replay, schedule)
    for record in by_op(replay).values():
        release = {"p": 0.0, "q": 40.0, "r": 15.0}[record.operation_id]
        assert record.gate_time == pytest.approx(release)
    # The same problem with every planned start at 0 still runs q at 40 and r at 15.
    assert_times(by_release["q"], 40, 40, 50)
    assert_times(by_release["r"], 15, 15, 25)


def _at_zero(a: Assignment) -> Assignment:
    length = a.end - a.start
    return at(a.operation_id, a.machine_id, 0, 0, length)


# --- slips right-shift successors (D-088) -----------------------------------------------------


def test_execute_slip_right_shifts_machine_and_job_successors_only() -> None:
    jobs, schedule = _slip_problem()
    compiled = compile_problem(problem(jobs, MACHINES))
    assert_verified(compiled, schedule)

    records = by_op(run_plan(compiled, schedule, slow(1.5)))

    # o1 runs 15 s instead of 10.
    assert_times(records["o1"], 0, 0, 15)
    assert records["o1"].run_seconds == pytest.approx(15.0)
    # Job successor o2 waits for o1's end; machine successor o3 waits for M1.
    assert_times(records["o2"], 15, 15, 25)
    assert_times(records["o3"], 15, 15, 35)
    # o4 follows o2 on M2, so it shifts by the same 5 s.
    assert_times(records["o4"], 25, 25, 30)
    assert records["o4"].machine_free_time == pytest.approx(25.0)
    assert records["o4"].gate_time == pytest.approx(0.0)
    # Unrelated ops, and o7 whose slack absorbs the slip, run as planned.
    assert_times(records["o5"], 0, 0, 8)
    assert_times(records["o6"], 8, 8, 12)
    assert_times(records["o7"], 40, 40, 50)


def test_execute_records_every_op_record_field() -> None:
    jobs, schedule = _slip_problem()
    compiled = compile_problem(problem(jobs, MACHINES))
    assert_verified(compiled, schedule)

    record = by_op(run_plan(compiled, schedule))["o2"]

    assert (record.operation_id, record.job_id, record.machine_id) == ("o2", "J1", "M2")
    assert record.batch_id is None
    assert (record.planned_start, record.planned_end) == pytest.approx((10.0, 20.0))
    assert record.gate_time == pytest.approx(10.0)
    assert record.machine_free_time == pytest.approx(0.0)
    assert (record.start, record.setup_end, record.end) == pytest.approx((10.0, 10.0, 20.0))
    assert record.setup_seconds == pytest.approx(0.0)
    assert record.run_seconds == pytest.approx(10.0)
    assert record.interrupted_seconds == 0.0
    assert record.overrun_seconds == 0.0
    assert record.max_lag_violation_seconds == 0.0
    assert record.pin_window_missed is False
    assert record.censored is False
    for name in ("operator_time", "calendar_time", "slip_cause"):
        assert hasattr(record, name), name


# --- PlanControl coverage check ----------------------------------------------------------------


def _two_op_shop() -> ShopModel:
    return build_shop(
        compile_problem(
            problem([job("J1", op("o1", "M1", 10)), job("J2", op("o2", "M2", 10))], MACHINES)
        )
    )


@pytest.mark.parametrize(
    "schedule",
    [
        pytest.param(plan(at("o1", "M1", 0, 0, 10)), id="missing-pending-op"),
        pytest.param(
            plan(at("o1", "M1", 0, 0, 10), at("o2", "M2", 0, 0, 10), at("o1", "M1", 10, 10, 20)),
            id="double-assigned-op",
        ),
        pytest.param(
            plan(at("o1", "M3", 0, 0, 10), at("o2", "M2", 0, 0, 10)), id="non-option-machine"
        ),
    ],
)
def test_plan_control_rejects_a_schedule_that_does_not_cover_pending_ops_once(
    schedule: Schedule,
) -> None:
    shop = _two_op_shop()
    with pytest.raises(ValueError):
        engine.execute(
            shop,
            engine.PlanControl(schedule),
            draws=engine.RepDraws(seed=1, replication=0, source=DeterministicSource()),
            outages=(),
            limits=engine.Limits.for_shop(shop),
        )


# --- termination and determinism ---------------------------------------------------------------


def test_execute_completed_run_reports_termination_end_time_and_no_downtime() -> None:
    jobs, schedule = _slip_problem()
    compiled = compile_problem(problem(jobs, MACHINES))

    run = run_plan(compiled, schedule)

    assert run.termination == "completed"
    assert run.end_time == pytest.approx(50.0)
    assert run.events > 0
    assert len(run.records) == len(schedule.assignments)
    assert all(not r.censored for r in run.records)
    assert all(value == 0 for value in run.downtime_by_machine.values())


def test_execute_is_deterministic() -> None:
    jobs, schedule = _slip_problem()
    compiled = compile_problem(problem(jobs, MACHINES))

    first = run_plan(compiled, schedule, slow(1.5))
    second = run_plan(compiled, schedule, slow(1.5))

    assert first.records == second.records
    assert (first.termination, first.end_time, first.events) == (
        second.termination,
        second.end_time,
        second.events,
    )
