"""Replication metrics, slip causes and evidence rows (T-181, REQ-135, REQ-141, COMP-136).

Decisions: D-097 (metrics come from the realized schedule through `objectives.evaluate`;
censored jobs count late) and D-098 (each slip has one binding cause).

API this suite pins for `twinflow.execution.metrics` (all pure, no I/O)::

    SLIP_CAUSES = ("none", "precedence", "machine", "operator", "calendar", "breakdown")
    METRIC_NAMES = ("on_time_pct", "weighted_tardiness", "total_tardiness", "tardy_count",
                    "makespan", "robustness", "total_setup", "max_start_slip_seconds",
                    "mean_start_slip_seconds", "max_completion_slip_seconds",
                    "bottleneck_utilization", "bottleneck_queue_length", "setup_hours",
                    "run_hours", "overrun_seconds", "max_lag_violations",
                    "pin_window_misses", "censored_jobs")      # SDD Data Model order

    slip_cause(record: OpRecord) -> str | None
    with_slip_causes(records: Iterable[OpRecord]) -> tuple[OpRecord, ...]
    realized_schedule(records: Iterable[OpRecord]) -> Schedule
    replication_metrics(compiled, shop, run: ExecutionRun, plan: Schedule | None)
        -> ReplicationMetrics
    operation_rows(plan_id: str, runs: Sequence[ExecutionRun], plan: Schedule | None)
        -> tuple[dict[str, Any], ...]
    job_rows(plan_id: str, shop: ShopModel, runs: Sequence[ExecutionRun],
             plan: Schedule | None) -> tuple[dict[str, Any], ...]

Slip cause (D-098, the D-034 binding-constraint rule):

* `None` when the record has no `planned_start` (dispatch control: nothing to slip from).
* `"none"` when `start <= planned_start + 1e-6`.
* Otherwise the cause whose time is the latest among the record's non-None
  `gate_time` (precedence), `machine_free_time` (machine), `machine_up_time` (breakdown),
  `calendar_time` (calendar) and `operator_time` (operator), counting only times
  `<= start + 1e-6` (a detached op's gate opens after its start and did not hold it back).
* Ties (times within 1e-6 of the latest) go to the earliest of that list, which is the order
  the executor waits in: the calendar and operator times are stamped at the moment the op
  could go, so when they equal an earlier constraint's time they did not bind.
* `with_slip_causes` returns new records (same order, `slip_cause` filled, every other field
  unchanged). `replication_metrics` and the row builders compute causes themselves; they do
  not need filled records.

Realized schedule (D-097): one `Assignment(operation_id, machine_id, start, setup_end, end,
batch_id)` per record, taken as recorded, in record order, in
`Schedule(status="FEASIBLE", solver="simulation", elapsed_seconds=0.0)`. The executor
already puts a censored op's `end` (and a never-started op's `start` and `setup_end`) at the
run's end time, which is T_c when the run stops at the calendar end.

`ReplicationMetrics` (frozen): every name in `METRIC_NAMES` as an attribute, plus
`objective_values: Mapping[ObjectiveKind, float]` (all thirteen,
`objectives.evaluate(compiled, realized_schedule(run.records), None)`),
`bottleneck_machine_id: str | None`, `machine_hours: Mapping[str, float]` (every shop
machine, 0.0 when idle) and `pool_utilization: Mapping[str, float]` (every shop pool), and
`metric_values() -> dict[str, float]` keyed exactly by `METRIC_NAMES`. Definitions:

* `weighted_tardiness`, `total_tardiness`, `tardy_count`, `makespan`, `robustness`,
  `total_setup` are the objective values of the same name (seconds where the objective is).
* `on_time_pct`: percent (0 to 100) of the jobs with a record and a due date that are not
  censored and have C_j <= d_j; 100.0 when no such job has a due date.
* Start slip of an op = max(0, start - planned_start); max and mean over records with a
  planned start (0.0 when none). Completion slip of a job = max(0, simulated C_j - planned
  C_j) with C_j the latest end of its ops; max over jobs (0.0 without a plan).
* Busy seconds of a machine = working seconds of its calendar covered by the union of its
  occupation intervals [start, end) (a batch counts once). Planned bottleneck = the machine
  with the largest planned busy share of its working seconds over [now, planned makespan
  end] (ties: lowest machine id). `bottleneck_utilization` = its realized busy share of its
  working seconds over [now, realized makespan end]. Without a plan the bottleneck is chosen
  by realized share over that window.
* `bottleneck_queue_length`: time average over [now, realized makespan end] of the number of
  records on the bottleneck waiting between `gate_time` and `start` (records whose gate never
  opened do not count).
* `setup_hours`, `run_hours`: sum of `setup_seconds`, `run_seconds` over units (a batch
  counts once) / 3600. `machine_hours[m]`: the same sum on machine m.
* `pool_utilization[p]` = held operator-seconds (quantity x the need's phase span: setup
  [start, setup_end), run [setup_end, end), both [start, end)) / (size x working seconds of
  the pool calendar over [now, realized makespan end]).
* `overrun_seconds`: sum of `overrun_seconds`. `max_lag_violations`: count of records with
  `max_lag_violation_seconds > 0`. `pin_window_misses`: count of `pin_window_missed`.
  `censored_jobs`: count of jobs with a censored record.

Evidence rows (SDD Data Model "Evidence rows (topic schedule)"): each row is a dict with
exactly the keys in `OP_ROW_KEYS` / `JOB_ROW_KEYS` below. Percentiles are
`numpy.percentile(values, q, method="linear")` over replications. Op rows come in the first
run's record order; job rows in order of the jobs' first record there. Optional fields
(`planned_*`, `due`, lateness percentiles, `on_time_share`) are present with value None when
there is no plan or no due date. `slip_*` use the start slip above; `causes` maps every
name in `SLIP_CAUSES` to its count across replications; `max_lag_violations`,
`pin_window_misses` and `censored_count` count replications. A job's lateness is C_j - d_j
in seconds (signed); its `on_time_share` is the share of replications in which it is not
censored and C_j <= d_j.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
from _engine_cases import (
    SLOW,
    after,
    assert_verified,
    at,
    by_op,
    compile_problem,
    need,
    op,
    plan,
    problem,
    run_plan,
    slow,
)
from _plans import BASELINE_GROUPS, GENERATORS

from twinflow.execution import engine, metrics
from twinflow.execution.metrics import OpRecord
from twinflow.execution.policy import FactorRule, LogNormal, ScriptedOutage, SimulationPolicy
from twinflow.execution.shop import ShopModel, build_shop, censor_time
from twinflow.execution.sources import DeterministicSource, ProfileSource
from twinflow.scheduling import objectives
from twinflow.scheduling.model import (
    Assignment,
    BatchCapacity,
    Calendar,
    CompiledProblem,
    Job,
    Machine,
    ObjectiveKind,
    Operation,
    OperatorPool,
    Schedule,
    SetupFunction,
)

EPS = 1e-6
HOUR = 3600.0
SDD_METRIC_NAMES = (
    "on_time_pct",
    "weighted_tardiness",
    "total_tardiness",
    "tardy_count",
    "makespan",
    "robustness",
    "total_setup",
    "max_start_slip_seconds",
    "mean_start_slip_seconds",
    "max_completion_slip_seconds",
    "bottleneck_utilization",
    "bottleneck_queue_length",
    "setup_hours",
    "run_hours",
    "overrun_seconds",
    "max_lag_violations",
    "pin_window_misses",
    "censored_jobs",
)
OP_ROW_KEYS = frozenset(
    {
        "row",
        "plan_id",
        "operation_id",
        "job_id",
        "machine_id",
        "planned_start",
        "planned_end",
        "start_p10",
        "start_p50",
        "start_p90",
        "end_p10",
        "end_p50",
        "end_p90",
        "slip_mean",
        "slip_max",
        "causes",
        "overrun_seconds_mean",
        "max_lag_violations",
        "pin_window_misses",
        "censored_count",
    }
)
JOB_ROW_KEYS = frozenset(
    {
        "row",
        "plan_id",
        "job_id",
        "due",
        "planned_completion",
        "completion_p10",
        "completion_p50",
        "completion_p90",
        "lateness_p10",
        "lateness_p50",
        "lateness_p90",
        "on_time_share",
        "censored_count",
    }
)
CAUSES = ("none", "precedence", "machine", "operator", "calendar", "breakdown")


# --- builders ------------------------------------------------------------------------------------


def rec(
    op_id: str,
    job_id: str,
    machine_id: str,
    start: float,
    end: float,
    *,
    planned: tuple[float, float] | None,
    setup_end: float | None = None,
    gate: float | None = 0.0,
    free: float | None = 0.0,
    operator: float | None = None,
    calendar: float | None = None,
    up: float | None = None,
    setup_seconds: float | None = None,
    run_seconds: float | None = None,
    batch_id: str | None = None,
    overrun: float = 0.0,
    lag: float = 0.0,
    pin_missed: bool = False,
    censored: bool = False,
) -> OpRecord:
    """A hand-built record; drawn seconds default to the recorded spans."""
    s_end = start if setup_end is None else setup_end
    return OpRecord(
        operation_id=op_id,
        job_id=job_id,
        machine_id=machine_id,
        batch_id=batch_id,
        planned_start=None if planned is None else planned[0],
        planned_end=None if planned is None else planned[1],
        gate_time=gate,
        machine_free_time=free,
        operator_time=operator,
        calendar_time=calendar,
        machine_up_time=up,
        start=start,
        setup_end=s_end,
        end=end,
        setup_seconds=s_end - start if setup_seconds is None else setup_seconds,
        run_seconds=end - s_end if run_seconds is None else run_seconds,
        interrupted_seconds=0.0,
        overrun_seconds=overrun,
        max_lag_violation_seconds=lag,
        pin_window_missed=pin_missed,
        censored=censored,
    )


def exec_run(
    records: Sequence[OpRecord], *, end_time: float | None = None, censored: bool = False
) -> engine.ExecutionRun:
    """A hand-built run around `records`."""
    return engine.ExecutionRun(
        records=tuple(records),
        downtime_by_machine={},
        termination="calendar_end" if censored else "completed",
        events=0,
        end_time=max(r.end for r in records) if end_time is None else end_time,
        horizon_violations=(),
    )


def due_job(job_id: str, *operations: Operation, due: float | None, weight: float = 1.0) -> Job:
    """An open job with a due date and an explicit weight."""
    return Job(id=job_id, due=due, weight_override=weight, operations=operations)


def assignments_of(records: Sequence[OpRecord]) -> tuple[Assignment, ...]:
    return tuple(
        Assignment(
            operation_id=r.operation_id,
            machine_id=r.machine_id,
            start=r.start,
            setup_end=r.setup_end,
            end=r.end,
            batch_id=r.batch_id,
        )
        for r in records
    )


def evaluate_records(compiled: CompiledProblem, records: Sequence[OpRecord]) -> dict[Any, float]:
    """The scheduler's objective values of the realized assignments (the D-097 oracle)."""
    realized = Schedule(
        status="FEASIBLE",
        assignments=assignments_of(records),
        solver="simulation",
        elapsed_seconds=0.0,
    )
    return objectives.evaluate(compiled, realized, None)


def assert_objectives_equal(got: Any, expected: Any) -> None:
    assert set(got) == set(ObjectiveKind)
    for kind in ObjectiveKind:
        assert got[kind] == pytest.approx(expected[kind], rel=1e-9, abs=EPS), kind


# --- the hand-computed 3-machine example ---------------------------------------------------------
#
# M1 always works. M2 works [0, 30) and [40, 200). M3 always works and sets up 3 s before every
# op. Pool P (size 2, no calendar): b1 holds one operator for both phases, c1 one for its run.
#
#   job  due  weight  op  machine  plan            realized (setup_end)        gate  free
#   J1   40   2       a1  M1       [0, 10)         [0, 15)  slowed             0     0
#                     a2  M2       [10, 30)        [40, 60) calendar pushed    15    10
#   J2   30   1       b1  M2       [0, 10)         [0, 10)                     0     0
#   J3   100  1       c1  M3       [0, 3, 8)       [0, 3, 8)                   0     0
#
# Planned makespan end 30: busy shares M1 10/30, M2 30/30, M3 8/30 -> bottleneck M2.
# Realized makespan end 60: M2 busy 10 + 20 = 30 of working 30 + 20 = 50 -> 0.6 (clock share
# would be 0.5). Queue on M2: a2 waits [15, 40) = 25 s, b1 0 s -> 25 / 60.
# Pool P: 10 (b1) + 5 (c1 run [3, 8)) = 15 operator-seconds of 2 x 60 -> 0.125.

M2_CAL = Calendar(id="C2", intervals=((0.0, 30.0), (40.0, 200.0)))
S3 = SetupFunction(id="S3", terms=(), default_seconds=3.0)
HAND_MACHINES = (
    Machine(id="M1", work_center_id="A"),
    Machine(id="M2", work_center_id="A", calendar_id="C2"),
    Machine(id="M3", work_center_id="A", setup_function_id="S3"),
)
POOL_P = OperatorPool(id="P", size=2)


def hand_example() -> tuple[CompiledProblem, ShopModel, Schedule, list[OpRecord]]:
    jobs = [
        due_job(
            "J1",
            op("a1", "M1", 10),
            op("a2", "M2", 20, predecessors=after("a1")),
            due=40.0,
            weight=2.0,
        ),
        due_job("J2", op("b1", "M2", 10, operator_needs=(need("P", "both"),)), due=30.0),
        due_job("J3", op("c1", "M3", 5, operator_needs=(need("P", "run"),)), due=100.0),
    ]
    compiled = compile_problem(
        problem(jobs, HAND_MACHINES, calendars=(M2_CAL,), pools=(POOL_P,), setup_functions=(S3,))
    )
    schedule = plan(
        at("a1", "M1", 0, 0, 10),
        at("b1", "M2", 0, 0, 10),
        at("a2", "M2", 10, 10, 30),
        at("c1", "M3", 0, 3, 8),
    )
    assert_verified(compiled, schedule)
    records = [
        rec("a1", "J1", "M1", 0, 15, planned=(0, 10)),
        rec("a2", "J1", "M2", 40, 60, planned=(10, 30), gate=15, free=10, calendar=40),
        rec("b1", "J2", "M2", 0, 10, planned=(0, 10), operator=0, calendar=0),
        rec("c1", "J3", "M3", 0, 8, planned=(0, 8), setup_end=3, operator=0),
    ]
    return compiled, build_shop(compiled), schedule, records


def hand_metrics(**changes: dict[str, Any]) -> Any:
    """Replication metrics of the hand example, with per-op record field changes."""
    compiled, shop, schedule, records = hand_example()
    changed = [dataclasses.replace(r, **changes.get(r.operation_id, {})) for r in records]
    return metrics.replication_metrics(compiled, shop, exec_run(changed), schedule)


def test_metric_names_are_the_sdd_list() -> None:
    assert tuple(metrics.METRIC_NAMES) == SDD_METRIC_NAMES
    assert tuple(metrics.SLIP_CAUSES) == CAUSES


def test_hand_example_objective_values_equal_evaluate_on_the_realized_schedule() -> None:
    compiled, shop, schedule, records = hand_example()

    result = metrics.replication_metrics(compiled, shop, exec_run(records), schedule)

    assert_objectives_equal(result.objective_values, evaluate_records(compiled, records))
    # By hand: J1 ends 60, due 40, weight 2 -> tardiness 20, weighted 40.
    assert result.total_tardiness == pytest.approx(20.0)
    assert result.weighted_tardiness == pytest.approx(40.0)
    assert result.tardy_count == pytest.approx(1.0)
    assert result.makespan == pytest.approx(60.0)
    assert result.total_setup == pytest.approx(3.0)
    # R(S) = (2(40-60) + (30-10) + (100-8)) / (2*40 + 30 + 100) = 72/210.
    assert result.robustness == pytest.approx(72.0 / 210.0)
    for name, kind in (
        ("weighted_tardiness", ObjectiveKind.WEIGHTED_TARDINESS),
        ("total_tardiness", ObjectiveKind.TOTAL_TARDINESS),
        ("tardy_count", ObjectiveKind.TARDY_COUNT),
        ("makespan", ObjectiveKind.MAKESPAN),
        ("robustness", ObjectiveKind.ROBUSTNESS),
        ("total_setup", ObjectiveKind.TOTAL_SETUP),
    ):
        assert getattr(result, name) == pytest.approx(result.objective_values[kind]), name


def test_hand_example_on_time_percent_counts_due_jobs() -> None:
    assert hand_metrics().on_time_pct == pytest.approx(100.0 * 2.0 / 3.0)


def test_hand_example_start_and_completion_slips() -> None:
    result = hand_metrics()

    # Only a2 starts late: 40 - 10 = 30; mean over four ops 7.5.
    assert result.max_start_slip_seconds == pytest.approx(30.0)
    assert result.mean_start_slip_seconds == pytest.approx(7.5)
    # J1 planned to complete at 30, completes at 60; J2 and J3 on plan.
    assert result.max_completion_slip_seconds == pytest.approx(30.0)


def test_hand_example_bottleneck_is_m2_with_utilization_over_working_time() -> None:
    result = hand_metrics()

    assert result.bottleneck_machine_id == "M2"
    assert result.bottleneck_utilization == pytest.approx(30.0 / 50.0)
    assert result.bottleneck_queue_length == pytest.approx(25.0 / 60.0)


def test_hand_example_setup_run_and_machine_hours_apart() -> None:
    result = hand_metrics()

    assert result.setup_hours == pytest.approx(3.0 / HOUR)
    assert result.run_hours == pytest.approx((15.0 + 20.0 + 10.0 + 5.0) / HOUR)
    assert dict(result.machine_hours) == pytest.approx(
        {"M1": 15.0 / HOUR, "M2": 30.0 / HOUR, "M3": 8.0 / HOUR}
    )


def test_hand_example_hours_read_drawn_seconds_not_clock_spans() -> None:
    # a1 was interrupted: clock span 15 s, but only 12 s of drawn run work.
    result = hand_metrics(a1={"run_seconds": 12.0})

    assert result.run_hours == pytest.approx((12.0 + 20.0 + 10.0 + 5.0) / HOUR)
    assert result.machine_hours["M1"] == pytest.approx(12.0 / HOUR)


def test_hand_example_pool_utilization() -> None:
    assert dict(hand_metrics().pool_utilization) == pytest.approx({"P": 15.0 / (2 * 60.0)})


def test_hand_example_violation_counts() -> None:
    result = hand_metrics(
        a1={"overrun_seconds": 4.0},
        c1={"overrun_seconds": 2.5, "pin_window_missed": True},
        a2={"max_lag_violation_seconds": 3.0, "pin_window_missed": True},
    )

    assert result.overrun_seconds == pytest.approx(6.5)
    assert result.max_lag_violations == 1
    assert result.pin_window_misses == 2
    assert result.censored_jobs == 0


def test_metric_values_hold_every_metric_name() -> None:
    result = hand_metrics()

    values = result.metric_values()

    assert tuple(values) == SDD_METRIC_NAMES
    for name in SDD_METRIC_NAMES:
        assert values[name] == pytest.approx(float(getattr(result, name))), name


def test_without_a_plan_slips_are_zero_and_the_bottleneck_is_the_realized_one() -> None:
    compiled, shop, _, records = hand_example()
    unplanned = [dataclasses.replace(r, planned_start=None, planned_end=None) for r in records]

    result = metrics.replication_metrics(compiled, shop, exec_run(unplanned), None)

    assert result.max_start_slip_seconds == 0.0
    assert result.mean_start_slip_seconds == 0.0
    assert result.max_completion_slip_seconds == 0.0
    # Realized shares over [0, 60]: M1 15/60, M2 30/50, M3 8/60.
    assert result.bottleneck_machine_id == "M2"
    assert result.bottleneck_utilization == pytest.approx(0.6)


def test_bottleneck_ties_go_to_the_lowest_machine_id() -> None:
    machines = (Machine(id="M2", work_center_id="A"), Machine(id="M1", work_center_id="A"))
    compiled = compile_problem(
        problem([due_job("J", op("x", "M2", 10), op("y", "M1", 10), due=None)], machines)
    )
    schedule = plan(at("x", "M2", 0, 0, 10), at("y", "M1", 0, 0, 10))
    assert_verified(compiled, schedule)
    records = [
        rec("x", "J", "M2", 0, 10, planned=(0, 10)),
        rec("y", "J", "M1", 0, 10, planned=(0, 10)),
    ]

    result = metrics.replication_metrics(
        compiled, build_shop(compiled), exec_run(records), schedule
    )

    assert result.bottleneck_machine_id == "M1"
    assert result.on_time_pct == pytest.approx(100.0)  # no due job: nothing is late


def test_a_batch_counts_once_in_hours_and_utilization() -> None:
    machines = (
        Machine(id="B1", work_center_id="A", batch=BatchCapacity(capacity=4.0, unit="pc")),
        Machine(id="M1", work_center_id="A"),
    )
    jobs = [
        due_job("JX", op("x", "B1", 40, batch_family="F"), due=None),
        due_job("JY", op("y", "B1", 40, batch_family="F"), due=None),
        due_job("JZ", op("z", "M1", 10), due=None),
    ]
    compiled = compile_problem(problem(jobs, machines))
    schedule = plan(
        at("x", "B1", 0, 0, 40, batch_id="K"),
        at("y", "B1", 0, 0, 40, batch_id="K"),
        at("z", "M1", 0, 0, 10),
    )
    assert_verified(compiled, schedule)
    records = [
        rec("x", "JX", "B1", 0, 40, planned=(0, 40), batch_id="K"),
        rec("y", "JY", "B1", 0, 40, planned=(0, 40), batch_id="K"),
        rec("z", "JZ", "M1", 0, 10, planned=(0, 10)),
    ]

    result = metrics.replication_metrics(
        compiled, build_shop(compiled), exec_run(records), schedule
    )

    assert result.machine_hours["B1"] == pytest.approx(40.0 / HOUR)
    assert result.run_hours == pytest.approx(50.0 / HOUR)
    assert result.bottleneck_machine_id == "B1"
    assert result.bottleneck_utilization == pytest.approx(1.0)


# --- censoring (D-097, D-107) --------------------------------------------------------------------
#
# M1 and M2 both work only [0, 30), so T_c = 30. a1 starts at 0 and is still running at T_c;
# b1 (after a1 on M1) never starts; c1 finishes. J2's due date is after T_c, yet it is late.

C30 = Calendar(id="C30", intervals=((0.0, 30.0),))


def censored_example() -> tuple[CompiledProblem, ShopModel, Schedule, list[OpRecord]]:
    machines = (
        Machine(id="M1", work_center_id="A", calendar_id="C30"),
        Machine(id="M2", work_center_id="A", calendar_id="C30"),
    )
    jobs = [
        due_job("J1", op("a1", "M1", 10), due=20.0),
        due_job("J2", op("b1", "M1", 10), due=1000.0),
        due_job("J3", op("c1", "M2", 5), due=50.0),
    ]
    compiled = compile_problem(problem(jobs, machines, calendars=(C30,)))
    schedule = plan(at("a1", "M1", 0, 0, 10), at("b1", "M1", 10, 10, 20), at("c1", "M2", 0, 0, 5))
    assert_verified(compiled, schedule)
    shop = build_shop(compiled)
    assert censor_time(shop, 20.0) == pytest.approx(30.0)
    records = [
        rec("a1", "J1", "M1", 0, 30, planned=(0, 10), run_seconds=100.0, censored=True),
        rec("b1", "J2", "M1", 30, 30, planned=(10, 20), free=None, censored=True),
        rec("c1", "J3", "M2", 0, 5, planned=(0, 5)),
    ]
    return compiled, shop, schedule, records


def test_realized_schedule_takes_censored_times_as_recorded() -> None:
    _, _, _, records = censored_example()

    realized = metrics.realized_schedule(records)

    assert realized.status == "FEASIBLE"
    assert realized.solver == "simulation"
    assert realized.assignments == assignments_of(records)
    b1 = realized.assignments[1]
    assert (b1.start, b1.setup_end, b1.end) == (30.0, 30.0, 30.0)


def test_censored_jobs_count_late_with_tardiness_to_the_censoring_time() -> None:
    compiled, shop, schedule, records = censored_example()

    result = metrics.replication_metrics(
        compiled, shop, exec_run(records, end_time=30.0, censored=True), schedule
    )

    assert result.censored_jobs == 2
    # Only J3 is on time; J2 is late although T_c = 30 is before its due date.
    assert result.on_time_pct == pytest.approx(100.0 / 3.0)
    # J1's tardiness is measured to T_c: 30 - 20.
    assert result.total_tardiness == pytest.approx(10.0)
    assert result.makespan == pytest.approx(30.0)
    assert result.max_start_slip_seconds == pytest.approx(20.0)  # b1: 30 - 10
    assert_objectives_equal(result.objective_values, evaluate_records(compiled, records))


# --- slip causes on hand-built records (D-098) ---------------------------------------------------


def late(**times: float | None) -> OpRecord:
    """An op planned at 10 that started at 20; the constraint times are given."""
    fields: dict[str, Any] = {"gate": None, "free": None} | times
    return rec("o", "J", "M1", 20, 30, planned=(10, 20), **fields)


@pytest.mark.parametrize(
    ("times", "cause"),
    [
        ({"gate": 20, "free": 5}, "precedence"),
        ({"gate": 5, "free": 20}, "machine"),
        ({"gate": 5, "free": 8, "operator": 20}, "operator"),
        ({"gate": 5, "free": 8, "calendar": 20}, "calendar"),
        ({"gate": 5, "free": 8, "up": 20}, "breakdown"),
    ],
)
def test_slip_cause_is_the_latest_constraint(times: dict[str, float], cause: str) -> None:
    assert metrics.slip_cause(late(**times)) == cause


def test_slip_cause_is_none_when_the_op_starts_on_time_within_tolerance() -> None:
    on_time = rec("o", "J", "M1", 10 + 0.5e-6, 20, planned=(10, 20), gate=10 + 0.5e-6)
    early = rec("o", "J", "M1", 8, 18, planned=(10, 20))

    assert metrics.slip_cause(on_time) == "none"
    assert metrics.slip_cause(early) == "none"


def test_slip_cause_just_past_tolerance_is_a_slip() -> None:
    record = rec("o", "J", "M1", 10 + 2e-6, 20, planned=(10, 20), gate=10 + 2e-6)

    assert metrics.slip_cause(record) == "precedence"


@pytest.mark.parametrize(
    ("times", "cause"),
    [
        # The calendar and operator stamps equal the moment the machine freed: machine binds.
        ({"gate": 5, "free": 20, "calendar": 20, "operator": 20}, "machine"),
        ({"gate": 20, "free": 20}, "precedence"),
        ({"gate": 5, "free": 8, "up": 20, "calendar": 20}, "breakdown"),
        ({"gate": 5, "free": 8, "calendar": 20, "operator": 20 + 0.5e-6}, "calendar"),
    ],
)
def test_slip_cause_ties_go_to_the_earlier_wait_stage(times: dict[str, float], cause: str) -> None:
    assert metrics.slip_cause(late(**times)) == cause


def test_slip_cause_ignores_a_gate_that_opened_after_the_start() -> None:
    # A detached op starts its setup at 20; its material gate opens at 25.
    assert metrics.slip_cause(late(gate=25, free=20)) == "machine"


def test_slip_cause_is_undefined_without_a_planned_start() -> None:
    assert metrics.slip_cause(rec("o", "J", "M1", 20, 30, planned=None, free=20)) is None


def test_with_slip_causes_fills_the_cause_and_keeps_everything_else() -> None:
    records = (late(gate=20, free=5), rec("p", "J", "M1", 10, 20, planned=(10, 20)))

    filled = metrics.with_slip_causes(records)

    assert isinstance(filled, tuple)
    assert [r.slip_cause for r in filled] == ["precedence", "none"]
    for before, after_ in zip(records, filled, strict=True):
        assert dataclasses.replace(after_, slip_cause=None) == before
    assert all(r.slip_cause is None for r in records)


# --- slip causes from real runs: five constructed cases ------------------------------------------


def _cause_of(run: Any, op_id: str) -> str | None:
    filled = {r.operation_id: r for r in metrics.with_slip_causes(run.records)}
    return filled[op_id].slip_cause


def test_real_run_precedence_slip() -> None:
    # p (M1) runs 15 s instead of 10; q (M2) waits for it.
    jobs = [
        due_job(
            "J",
            op("p", "M1", 10, quantity=SLOW),
            op("q", "M2", 10, predecessors=after("p")),
            due=None,
        )
    ]
    compiled = compile_problem(problem(jobs, HAND_MACHINES[:2], calendars=(M2_CAL,)))
    schedule = plan(at("p", "M1", 0, 0, 10), at("q", "M2", 10, 10, 20))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(1.5))

    q = by_op(run)["q"]
    assert (q.start, q.gate_time, q.machine_free_time) == pytest.approx((15.0, 15.0, 0.0))
    assert _cause_of(run, "q") == "precedence"
    assert _cause_of(run, "p") == "none"


def test_real_run_machine_slip_on_a_calendared_machine() -> None:
    # o1 runs 15 s instead of 10 on M2; o2 (another job) waits for the machine. M2's calendar
    # allows 15 at once, so calendar_time equals the machine-free time and must not win.
    jobs = [
        due_job("J1", op("o1", "M2", 10, quantity=SLOW), due=None),
        due_job("J2", op("o2", "M2", 10), due=None),
    ]
    compiled = compile_problem(problem(jobs, HAND_MACHINES[:2], calendars=(M2_CAL,)))
    schedule = plan(at("o1", "M2", 0, 0, 10), at("o2", "M2", 10, 10, 20))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(1.5))

    o2 = by_op(run)["o2"]
    assert (o2.start, o2.machine_free_time, o2.calendar_time) == pytest.approx((15.0, 15.0, 15.0))
    assert _cause_of(run, "o2") == "machine"


def test_real_run_operator_slip() -> None:
    # One operator: A (M1) holds it 15 s instead of 10; B (M2) waits for the grant.
    jobs = [
        due_job(
            "JA", op("A", "M1", 10, quantity=SLOW, operator_needs=(need("P", "both"),)), due=None
        ),
        due_job("JB", op("B", "M2", 10, operator_needs=(need("P", "both"),)), due=None),
    ]
    machines = (Machine(id="M1", work_center_id="A"), Machine(id="M2", work_center_id="A"))
    compiled = compile_problem(problem(jobs, machines, pools=(OperatorPool(id="P", size=1),)))
    schedule = plan(at("A", "M1", 0, 0, 10), at("B", "M2", 10, 10, 20))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(1.5))

    b = by_op(run)["B"]
    assert (b.start, b.operator_time, b.machine_free_time) == pytest.approx((15.0, 15.0, 0.0))
    assert _cause_of(run, "B") == "operator"


def test_real_run_operator_granted_at_once_does_not_own_a_machine_slip() -> None:
    jobs = [
        due_job(
            "J1", op("o1", "M1", 10, quantity=SLOW, operator_needs=(need("P", "both"),)), due=None
        ),
        due_job("J2", op("o2", "M1", 10, operator_needs=(need("P", "both"),)), due=None),
    ]
    machines = (Machine(id="M1", work_center_id="A"),)
    compiled = compile_problem(problem(jobs, machines, pools=(OperatorPool(id="P", size=5),)))
    schedule = plan(at("o1", "M1", 0, 0, 10), at("o2", "M1", 10, 10, 20))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(1.5))

    o2 = by_op(run)["o2"]
    assert (o2.start, o2.operator_time, o2.machine_free_time) == pytest.approx((15.0, 15.0, 15.0))
    assert _cause_of(run, "o2") == "machine"


def test_real_run_calendar_slip() -> None:
    # M2 works [0, 18) and [30, 100). o1 runs 15 s instead of 10; o2 (8 s, forbid) no longer
    # fits before 18 and waits for 30.
    cal = Calendar(id="CB", intervals=((0.0, 18.0), (30.0, 100.0)))
    machines = (Machine(id="M2", work_center_id="A", calendar_id="CB"),)
    jobs = [
        due_job("J1", op("o1", "M2", 10, quantity=SLOW), due=None),
        due_job("J2", op("o2", "M2", 8), due=None),
    ]
    compiled = compile_problem(problem(jobs, machines, calendars=(cal,)))
    schedule = plan(at("o1", "M2", 0, 0, 10), at("o2", "M2", 10, 10, 18))
    assert_verified(compiled, schedule)

    run = run_plan(compiled, schedule, slow(1.5))

    o2 = by_op(run)["o2"]
    assert (o2.start, o2.calendar_time, o2.machine_free_time) == pytest.approx((30.0, 30.0, 15.0))
    assert _cause_of(run, "o2") == "calendar"


def test_real_run_breakdown_slip() -> None:
    # M1 is down [12, 25); o2 planned at 20 starts when M1 comes up at 25.
    machines = (Machine(id="M1", work_center_id="A"),)
    jobs = [
        due_job("J1", op("o1", "M1", 10), due=None),
        due_job("J2", op("o2", "M1", 10), due=None),
    ]
    compiled = compile_problem(problem(jobs, machines))
    schedule = plan(at("o1", "M1", 0, 0, 10), at("o2", "M1", 20, 20, 30))
    assert_verified(compiled, schedule)
    shop = build_shop(compiled)

    run = engine.execute(
        shop,
        engine.PlanControl(schedule),
        draws=engine.RepDraws(seed=1, replication=0, source=DeterministicSource()),
        outages=(ScriptedOutage(machine_id="M1", start=12.0, duration=13.0),),
        limits=engine.Limits.for_shop(shop),
    )

    o2 = by_op(run)["o2"]
    assert (o2.start, o2.machine_up_time, o2.machine_free_time) == pytest.approx((25.0, 25.0, 10.0))
    assert _cause_of(run, "o2") == "breakdown"
    assert _cause_of(run, "o1") == "none"


# --- real runs: realized objectives (D-097) -------------------------------------------------------


@pytest.mark.parametrize("group", BASELINE_GROUPS)
def test_deterministic_replay_metrics_equal_the_plan(group: str) -> None:
    for seed in range(3):
        generated = GENERATORS[group](seed)
        shop = build_shop(generated.compiled)
        run = run_plan(generated.compiled, generated.schedule)

        result = metrics.replication_metrics(generated.compiled, shop, run, generated.schedule)

        planned = objectives.evaluate(generated.compiled, generated.schedule, None)
        assert_objectives_equal(result.objective_values, planned)
        assert result.max_start_slip_seconds == pytest.approx(0.0, abs=EPS), (group, seed)
        assert result.max_completion_slip_seconds == pytest.approx(0.0, abs=EPS), (group, seed)
        assert result.censored_jobs == 0
        assert result.on_time_pct == pytest.approx(_planned_on_time_pct(shop, generated.schedule))
        assert {r.slip_cause for r in metrics.with_slip_causes(run.records)} == {"none"}


def _planned_on_time_pct(shop: ShopModel, schedule: Schedule) -> float:
    completion: dict[str, float] = {}
    for a in schedule.assignments:
        job_id = shop.operations[a.operation_id].job_id
        completion[job_id] = max(completion.get(job_id, a.end), a.end)
    due = {j: shop.jobs[j].due for j in completion if shop.jobs[j].due is not None}
    if not due:
        return 100.0
    on_time = sum(1 for j, d in due.items() if d is not None and completion[j] <= d)
    return 100.0 * on_time / len(due)


def test_stochastic_run_objectives_equal_evaluate_on_the_realized_assignments() -> None:
    generated = GENERATORS["mixed"](0)
    policy = SimulationPolicy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=LogNormal(mean=1.2, cv=0.4)),),
    )
    shop = build_shop(generated.compiled)
    run = engine.execute(
        shop,
        engine.PlanControl(generated.schedule),
        draws=engine.RepDraws(seed=7, replication=3, source=ProfileSource(policy)),
        outages=(),
        limits=engine.Limits.for_shop(shop),
    )
    assert any(r.start > r.planned_start + EPS for r in run.records if r.planned_start is not None)

    result = metrics.replication_metrics(generated.compiled, shop, run, generated.schedule)

    assert metrics.realized_schedule(run.records).assignments == assignments_of(run.records)
    assert_objectives_equal(
        result.objective_values, evaluate_records(generated.compiled, run.records)
    )
    assert result.max_start_slip_seconds > 0.0


# --- evidence rows across replications (REQ-141) -------------------------------------------------
#
# J1: p (M1, plan [0, 10)) then q (M2, plan [10, 20)), due 25. J2: r (M3, plan [0, 5)), no due.
#   rep  p            q                                   C_J1
#   0    [0, 10)      [10, 20)  on time                    20
#   1    [0, 14)      [14, 24)  gate 14 -> precedence      24   q lag violation; p pin missed
#   2    [0, 10)      [19, 29)  M2 free 19 -> machine      29   q overrun 3
# numpy linear percentiles of three values v0 <= v1 <= v2: p10 = v0 + 0.2 (v1 - v0),
# p50 = v1, p90 = v1 + 0.8 (v2 - v1).


def rows_example() -> tuple[ShopModel, Schedule, list[engine.ExecutionRun]]:
    machines = tuple(Machine(id=m, work_center_id="A") for m in ("M1", "M2", "M3"))
    jobs = [
        due_job("J1", op("p", "M1", 10), op("q", "M2", 10, predecessors=after("p")), due=25.0),
        due_job("J2", op("r", "M3", 5), due=None),
    ]
    compiled = compile_problem(problem(jobs, machines))
    schedule = plan(at("p", "M1", 0, 0, 10), at("q", "M2", 10, 10, 20), at("r", "M3", 0, 0, 5))
    assert_verified(compiled, schedule)

    def rep(p: OpRecord, q: OpRecord) -> engine.ExecutionRun:
        return exec_run([p, q, rec("r", "J2", "M3", 0, 5, planned=(0, 5))])

    runs = [
        rep(
            rec("p", "J1", "M1", 0, 10, planned=(0, 10)),
            rec("q", "J1", "M2", 10, 20, planned=(10, 20), gate=10),
        ),
        rep(
            rec("p", "J1", "M1", 0, 14, planned=(0, 10), pin_missed=True),
            rec("q", "J1", "M2", 14, 24, planned=(10, 20), gate=14, lag=2.0),
        ),
        rep(
            rec("p", "J1", "M1", 0, 10, planned=(0, 10)),
            rec("q", "J1", "M2", 19, 29, planned=(10, 20), gate=10, free=19, overrun=3.0),
        ),
    ]
    return build_shop(compiled), schedule, runs


def _causes(**counts: int) -> dict[str, int]:
    return {cause: counts.get(cause, 0) for cause in CAUSES}


def test_operation_rows_have_exactly_the_sdd_fields_in_record_order() -> None:
    _, schedule, runs = rows_example()

    rows = metrics.operation_rows("plan-a", runs, schedule)

    assert [r["operation_id"] for r in rows] == ["p", "q", "r"]
    for row in rows:
        assert set(row) == OP_ROW_KEYS
        assert row["row"] == "operation"
        assert row["plan_id"] == "plan-a"


def test_operation_row_percentiles_slips_causes_and_violations() -> None:
    _, schedule, runs = rows_example()

    q = metrics.operation_rows("plan-a", runs, schedule)[1]

    assert (q["job_id"], q["machine_id"]) == ("J1", "M2")
    assert (q["planned_start"], q["planned_end"]) == pytest.approx((10.0, 20.0))
    # starts 10, 14, 19; ends 20, 24, 29.
    assert (q["start_p10"], q["start_p50"], q["start_p90"]) == pytest.approx((10.8, 14.0, 18.0))
    assert (q["end_p10"], q["end_p50"], q["end_p90"]) == pytest.approx((20.8, 24.0, 28.0))
    assert np.percentile([10.0, 14.0, 19.0], [10, 50, 90]) == pytest.approx([10.8, 14.0, 18.0])
    assert q["slip_mean"] == pytest.approx(13.0 / 3.0)
    assert q["slip_max"] == pytest.approx(9.0)
    assert q["causes"] == _causes(none=1, precedence=1, machine=1)
    assert q["overrun_seconds_mean"] == pytest.approx(1.0)
    assert q["max_lag_violations"] == 1
    assert q["pin_window_misses"] == 0
    assert q["censored_count"] == 0


def test_operation_row_of_an_op_that_ran_long_but_started_on_time() -> None:
    _, schedule, runs = rows_example()

    p = metrics.operation_rows("plan-a", runs, schedule)[0]

    # ends 10, 14, 10 -> sorted 10, 10, 14.
    assert (p["end_p10"], p["end_p50"], p["end_p90"]) == pytest.approx((10.0, 10.0, 13.2))
    assert (p["slip_mean"], p["slip_max"]) == pytest.approx((0.0, 0.0))
    assert p["causes"] == _causes(none=3)
    assert p["pin_window_misses"] == 1


def test_job_rows_have_completion_and_signed_lateness_percentiles() -> None:
    shop, schedule, runs = rows_example()

    rows = metrics.job_rows("plan-a", shop, runs, schedule)

    assert [r["job_id"] for r in rows] == ["J1", "J2"]
    for row in rows:
        assert set(row) == JOB_ROW_KEYS
        assert row["row"] == "job"
        assert row["plan_id"] == "plan-a"
    j1, j2 = rows
    assert j1["due"] == pytest.approx(25.0)
    assert j1["planned_completion"] == pytest.approx(20.0)
    # completions 20, 24, 29; lateness -5, -1, 4.
    assert (j1["completion_p10"], j1["completion_p50"], j1["completion_p90"]) == pytest.approx(
        (20.8, 24.0, 28.0)
    )
    assert (j1["lateness_p10"], j1["lateness_p50"], j1["lateness_p90"]) == pytest.approx(
        (-4.2, -1.0, 3.0)
    )
    assert j1["on_time_share"] == pytest.approx(2.0 / 3.0)
    assert j1["censored_count"] == 0
    assert j2["due"] is None
    assert j2["planned_completion"] == pytest.approx(5.0)
    assert (j2["completion_p10"], j2["completion_p50"], j2["completion_p90"]) == pytest.approx(
        (5.0, 5.0, 5.0)
    )
    assert (j2["lateness_p10"], j2["lateness_p50"], j2["lateness_p90"]) == (None, None, None)
    assert j2["on_time_share"] is None


def test_rows_count_a_censored_replication_late() -> None:
    shop, schedule, runs = rows_example()
    # Two replications: rep 0 as above, and one where q is still running at T_c = 30.
    censored_q = rec("q", "J1", "M2", 10, 30, planned=(10, 20), gate=10, censored=True)
    replaced = [
        runs[0],
        exec_run([runs[0].records[0], censored_q, runs[0].records[2]], censored=True),
    ]

    q_row = metrics.operation_rows("plan-a", replaced, schedule)[1]
    j1_row = metrics.job_rows("plan-a", shop, replaced, schedule)[0]

    assert q_row["censored_count"] == 1
    assert j1_row["censored_count"] == 1
    assert j1_row["on_time_share"] == pytest.approx(0.5)
    assert j1_row["completion_p50"] == pytest.approx(25.0)


def test_rows_count_a_censored_job_late_even_before_its_due_date() -> None:
    shop, schedule, runs = rows_example()
    # J1 due 25; a censored run stopping at T_c = 22 is still late.
    censored_q = rec("q", "J1", "M2", 10, 22, planned=(10, 20), gate=10, censored=True)
    run = exec_run([runs[0].records[0], censored_q, runs[0].records[2]], censored=True)

    j1 = metrics.job_rows("plan-a", shop, [run], schedule)[0]

    assert j1["on_time_share"] == pytest.approx(0.0)
    assert j1["lateness_p50"] == pytest.approx(-3.0)


def test_rows_without_a_plan_have_no_planned_times() -> None:
    shop, _, runs = rows_example()
    unplanned = [
        exec_run(
            [dataclasses.replace(r, planned_start=None, planned_end=None) for r in run.records]
        )
        for run in runs
    ]

    op_rows = metrics.operation_rows("baseline", unplanned, None)
    job_rows = metrics.job_rows("baseline", shop, unplanned, None)

    assert all(r["planned_start"] is None and r["planned_end"] is None for r in op_rows)
    assert all(r["planned_completion"] is None for r in job_rows)
    assert job_rows[0]["completion_p50"] == pytest.approx(24.0)
