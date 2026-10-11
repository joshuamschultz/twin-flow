"""Per-operation execution records and replication metrics (COMP-136, D-097, D-098).

`OpRecord` is the one interface between a run and every metric (the D-017 rule): the
executor writes one per pending operation, and replication metrics read only these. It
is a slotted frozen dataclass built once per operation at the end of a run, so a
5,000-operation replication pays no validation cost.

Times are problem seconds. A censored operation (the run stopped before it finished) has
`end` at the run's end time; one that never started also has `start` and `setup_end`
there and zero drawn seconds.

Every objective value comes from `objectives.evaluate` on the realized schedule (D-097):
this module never recomputes tardiness. Working time comes from `calendar_math` only.
`replication_metrics` builds its indexes once per run and constructs no pydantic model
except the realized `Schedule`, so it stays linear in the number of records.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np

from twinflow.execution.shop import ShopModel
from twinflow.scheduling import objectives
from twinflow.scheduling.calendar_math import Interval, Intervals, work_seconds
from twinflow.scheduling.model import Assignment, CompiledProblem, ObjectiveKind, Schedule

if TYPE_CHECKING:
    from twinflow.execution.engine import ExecutionRun

SLIP_CAUSES = ("none", "precedence", "machine", "operator", "calendar", "breakdown")
"""Every value `slip_cause` returns besides None (D-098)."""

METRIC_NAMES = (
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
"""Replication metric names in the SDD Data Model order."""

SLIP_TOLERANCE = 1e-6
"""A start within this many seconds of its planned start (or constraint) is on time."""

SECONDS_PER_HOUR = 3600.0
PERCENTILES = (10.0, 50.0, 90.0)

_CAUSE_FIELDS = (
    ("precedence", "gate_time"),
    ("machine", "machine_free_time"),
    ("breakdown", "machine_up_time"),
    ("calendar", "calendar_time"),
    ("operator", "operator_time"),
)
"""Constraint times in the order the executor waits for them (the D-098 tie order)."""


@dataclass(frozen=True, slots=True)
class OpRecord:
    """What happened to one operation in one replication.

    The binding-constraint times feed the slip cause (D-098, filled by metrics):
    `gate_time` (material gate: release, ready time, every predecessor link; None when it
    never opened), `machine_free_time` (previous unit's end, else max(now,
    available_from)), `operator_time` (latest operator grant; None without operator
    needs), `calendar_time` (earliest start the calendar rule allowed; None without a
    calendar) and `machine_up_time` (end of the outage that held the start back; None when
    none did). `setup_seconds` and `run_seconds` are drawn work (planned x factor) and
    exclude downtime; `interrupted_seconds` is the downtime that stopped its work.
    """

    operation_id: str
    job_id: str
    machine_id: str
    batch_id: str | None
    planned_start: float | None
    planned_end: float | None
    gate_time: float | None
    machine_free_time: float | None
    operator_time: float | None
    calendar_time: float | None
    machine_up_time: float | None
    start: float
    setup_end: float
    end: float
    setup_seconds: float
    run_seconds: float
    interrupted_seconds: float
    overrun_seconds: float
    max_lag_violation_seconds: float
    pin_window_missed: bool
    censored: bool
    slip_cause: str | None = None


@dataclass(frozen=True, slots=True)
class ReplicationMetrics:
    """The metrics of one replication (SDD Data Model, D-097).

    Every name in `METRIC_NAMES` is an attribute. `objective_values` holds all thirteen
    `objectives.evaluate` values of the realized schedule; `machine_hours` covers every
    shop machine and `pool_utilization` every shop pool.
    """

    on_time_pct: float
    weighted_tardiness: float
    total_tardiness: float
    tardy_count: float
    makespan: float
    robustness: float
    total_setup: float
    max_start_slip_seconds: float
    mean_start_slip_seconds: float
    max_completion_slip_seconds: float
    bottleneck_utilization: float
    bottleneck_queue_length: float
    setup_hours: float
    run_hours: float
    overrun_seconds: float
    max_lag_violations: int
    pin_window_misses: int
    censored_jobs: int
    objective_values: Mapping[ObjectiveKind, float]
    bottleneck_machine_id: str | None
    machine_hours: Mapping[str, float]
    pool_utilization: Mapping[str, float]

    def metric_values(self) -> dict[str, float]:
        """Every metric as a float, keyed exactly by `METRIC_NAMES` in that order."""
        return {name: float(getattr(self, name)) for name in METRIC_NAMES}


# --- slip causes (D-098) -------------------------------------------------------------------


def slip_cause(record: OpRecord) -> str | None:
    """The binding constraint of a late start (D-098, the D-034 rule).

    None without a planned start; `"none"` when the op started within `SLIP_TOLERANCE` of
    it. Otherwise the latest constraint time at or before the start wins, ties going to the
    earlier wait stage (`_CAUSE_FIELDS` order). Gap rule (orchestrator decision): when no
    constraint time is at or before the start, the latest constraint time it has gives the
    cause; with no constraint time at all the gate never opened, which in a record means
    unfinished predecessors, so the cause is `"precedence"`. Refinement (T-184): an op that
    never started (its start is the censoring time) and whose gate opened was not held by
    its gate, so the gate is no candidate; with no other constraint time its machine never
    reached it, so the cause is `"machine"`.
    """
    if record.planned_start is None:
        return None
    if record.start <= record.planned_start + SLIP_TOLERANCE:
        return "none"
    fields: tuple[tuple[str, str], ...] = _CAUSE_FIELDS
    if _never_started(record) and record.gate_time is not None:
        fields = tuple(field for field in _CAUSE_FIELDS if field[1] != "gate_time")
        if all(getattr(record, name) is None for _, name in fields):
            return "machine"
    times = [(cause, time) for cause, name in fields if (time := getattr(record, name)) is not None]
    binding = [(cause, time) for cause, time in times if time <= record.start + SLIP_TOLERANCE]
    candidates = binding or times
    if not candidates:
        return "precedence"
    latest = max(time for _, time in candidates)
    return next(cause for cause, time in candidates if time >= latest - SLIP_TOLERANCE)


def _never_started(record: OpRecord) -> bool:
    """A censored record with no elapsed and no drawn time: no machine started it."""
    return (
        record.censored
        and record.start == record.setup_end == record.end
        and record.setup_seconds == 0.0
        and record.run_seconds == 0.0
    )


def with_slip_causes(records: Iterable[OpRecord]) -> tuple[OpRecord, ...]:
    """New records in the same order with `slip_cause` filled; nothing else changes."""
    return tuple(replace(r, slip_cause=slip_cause(r)) for r in records)


def _start_slip(record: OpRecord) -> float | None:
    """max(0, start - planned start), or None without a planned start."""
    if record.planned_start is None:
        return None
    return max(0.0, record.start - record.planned_start)


# --- realized schedule and objective values (D-097) ----------------------------------------


def realized_schedule(records: Iterable[OpRecord]) -> Schedule:
    """One assignment per record, times as recorded, in record order (D-097)."""
    return Schedule(
        status="FEASIBLE",
        assignments=tuple(
            Assignment(
                operation_id=r.operation_id,
                machine_id=r.machine_id,
                start=r.start,
                setup_end=r.setup_end,
                end=r.end,
                batch_id=r.batch_id,
            )
            for r in records
        ),
        solver="simulation",
        elapsed_seconds=0.0,
    )


# --- machine occupation and busy share -----------------------------------------------------


def machine_occupation(
    spans: Iterable[tuple[str, float, float]],
) -> dict[str, tuple[Interval, ...]]:
    """Each machine's occupied time as merged `[start, end)` intervals.

    `spans` are `(machine_id, start, end)`; overlapping or touching spans merge, so a batch
    (members share one span) counts once. Empty spans are dropped.
    """
    by_machine: dict[str, list[Interval]] = {}
    for machine_id, start, end in spans:
        if end > start:
            by_machine.setdefault(machine_id, []).append((start, end))
    return {machine_id: _merged(spans_) for machine_id, spans_ in by_machine.items()}


def _merged(intervals: list[Interval]) -> tuple[Interval, ...]:
    intervals.sort()
    merged: list[Interval] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def busy_seconds(
    calendar: Intervals, occupied: Sequence[Interval], start: float, end: float
) -> float:
    """Working seconds of `calendar` covered by `occupied`, clipped to `[start, end)`."""
    total = 0.0
    for occupied_start, occupied_end in occupied:
        lo, hi = max(occupied_start, start), min(occupied_end, end)
        if hi > lo:
            total += work_seconds(calendar, lo, hi)
    return total


def busy_shares(
    shop: ShopModel, occupation: Mapping[str, Sequence[Interval]], end: float
) -> dict[str, float]:
    """Every shop machine's busy share of its working seconds over `[now, end]`.

    0.0 for a machine without working time in the window.
    """
    shares: dict[str, float] = {}
    for machine_id, machine in shop.machines.items():
        working = work_seconds(machine.calendar, shop.now, end) if end > shop.now else 0.0
        occupied = occupation.get(machine_id, ())
        busy = busy_seconds(machine.calendar, occupied, shop.now, end)
        shares[machine_id] = busy / working if working > 0 else 0.0
    return shares


def plan_occupation(plan: Schedule) -> dict[str, tuple[Interval, ...]]:
    """The plan's merged occupation per machine."""
    return machine_occupation((a.machine_id, a.start, a.end) for a in plan.assignments)


def plan_end(shop: ShopModel, plan: Schedule) -> float:
    """The plan's last end (its makespan end), `now` for an empty plan."""
    return max((a.end for a in plan.assignments), default=shop.now)


def top_machine(shares: Mapping[str, float]) -> str | None:
    """The machine with the largest share, ties to the lowest id; None without machines."""
    if not shares:
        return None
    return min(shares, key=lambda machine_id: (-shares[machine_id], machine_id))


# --- replication metrics --------------------------------------------------------------------


def replication_metrics(
    compiled: CompiledProblem, shop: ShopModel, run: ExecutionRun, plan: Schedule | None
) -> ReplicationMetrics:
    """The metrics of one replication (COMP-136, D-097).

    Objective values are `objectives.evaluate` of the realized schedule; censored jobs
    count late. The bottleneck is the planned one (largest planned busy share), else the
    realized one when there is no plan.
    """
    records = run.records
    values = objectives.evaluate(compiled, realized_schedule(records), None)
    end = max((r.end for r in records), default=shop.now)
    occupation = machine_occupation((r.machine_id, r.start, r.end) for r in records)
    realized_shares = busy_shares(shop, occupation, end)
    bottleneck = _bottleneck(shop, plan, realized_shares)
    slips = [slip for r in records if (slip := _start_slip(r)) is not None]
    setup_seconds, run_seconds, machine_seconds = _unit_seconds(shop, records)
    jobs = _JobOutcomes.of(records)
    return ReplicationMetrics(
        on_time_pct=jobs.on_time_pct(shop),
        weighted_tardiness=values[ObjectiveKind.WEIGHTED_TARDINESS],
        total_tardiness=values[ObjectiveKind.TOTAL_TARDINESS],
        tardy_count=values[ObjectiveKind.TARDY_COUNT],
        makespan=values[ObjectiveKind.MAKESPAN],
        robustness=values[ObjectiveKind.ROBUSTNESS],
        total_setup=values[ObjectiveKind.TOTAL_SETUP],
        max_start_slip_seconds=max(slips, default=0.0),
        mean_start_slip_seconds=sum(slips) / len(slips) if slips else 0.0,
        max_completion_slip_seconds=_max_completion_slip(shop, jobs, plan),
        bottleneck_utilization=0.0 if bottleneck is None else realized_shares[bottleneck],
        bottleneck_queue_length=_queue_length(records, bottleneck, shop.now, end),
        setup_hours=setup_seconds / SECONDS_PER_HOUR,
        run_hours=run_seconds / SECONDS_PER_HOUR,
        overrun_seconds=sum(r.overrun_seconds for r in records),
        max_lag_violations=sum(1 for r in records if r.max_lag_violation_seconds > 0),
        pin_window_misses=sum(1 for r in records if r.pin_window_missed),
        censored_jobs=len(jobs.censored),
        objective_values=values,
        bottleneck_machine_id=bottleneck,
        machine_hours={m: s / SECONDS_PER_HOUR for m, s in machine_seconds.items()},
        pool_utilization=_pool_utilization(shop, records, end),
    )


@dataclass(frozen=True, slots=True)
class _JobOutcomes:
    """Each job's completion (latest end of its records) and the censored job ids, in the
    order of each job's first record."""

    completion: dict[str, float]
    censored: frozenset[str]

    @classmethod
    def of(cls, records: Iterable[OpRecord]) -> _JobOutcomes:
        completion: dict[str, float] = {}
        censored: set[str] = set()
        for r in records:
            previous = completion.get(r.job_id)
            completion[r.job_id] = r.end if previous is None else max(previous, r.end)
            if r.censored:
                censored.add(r.job_id)
        return cls(completion, frozenset(censored))

    def on_time(self, job_id: str, due: float) -> bool:
        """Not censored and C_j <= d_j (D-097: censored jobs count late)."""
        return job_id not in self.censored and self.completion[job_id] <= due

    def on_time_pct(self, shop: ShopModel) -> float:
        """Percent of due jobs on time; 100.0 when no recorded job has a due date."""
        due = {j: d for j in self.completion if (d := shop.jobs[j].due) is not None}
        if not due:
            return 100.0
        return 100.0 * sum(1 for j, d in due.items() if self.on_time(j, d)) / len(due)


def _bottleneck(
    shop: ShopModel, plan: Schedule | None, realized_shares: Mapping[str, float]
) -> str | None:
    """Largest planned busy share over [now, planned end]; realized share without a plan."""
    if plan is None:
        return top_machine(realized_shares)
    return top_machine(busy_shares(shop, plan_occupation(plan), plan_end(shop, plan)))


def _queue_length(
    records: Iterable[OpRecord], machine_id: str | None, now: float, end: float
) -> float:
    """Time-average number of the machine's records between gate open and start."""
    if machine_id is None or end <= now:
        return 0.0
    waiting = sum(
        max(0.0, r.start - r.gate_time)
        for r in records
        if r.machine_id == machine_id and r.gate_time is not None
    )
    return waiting / (end - now)


def _unit_seconds(
    shop: ShopModel, records: Iterable[OpRecord]
) -> tuple[float, float, dict[str, float]]:
    """Drawn setup seconds, run seconds and per-machine seconds; a batch counts once."""
    setup = run = 0.0
    machine_seconds = dict.fromkeys(shop.machines, 0.0)
    seen_batches: set[tuple[str, str]] = set()
    for r in records:
        if r.batch_id is not None:
            if (r.machine_id, r.batch_id) in seen_batches:
                continue
            seen_batches.add((r.machine_id, r.batch_id))
        setup += r.setup_seconds
        run += r.run_seconds
        machine_seconds[r.machine_id] = (
            machine_seconds.get(r.machine_id, 0.0) + r.setup_seconds + r.run_seconds
        )
    return setup, run, machine_seconds


def _max_completion_slip(shop: ShopModel, jobs: _JobOutcomes, plan: Schedule | None) -> float:
    """max over jobs of max(0, simulated C_j - planned C_j); 0.0 without a plan."""
    if plan is None:
        return 0.0
    planned = planned_completions(shop, plan)
    return max(
        (
            max(0.0, completion - planned[job_id])
            for job_id, completion in jobs.completion.items()
            if job_id in planned
        ),
        default=0.0,
    )


def planned_completions(shop: ShopModel, plan: Schedule) -> dict[str, float]:
    """Each planned job's latest planned end (pending operations of the shop only)."""
    completion: dict[str, float] = {}
    for a in plan.assignments:
        op = shop.operations.get(a.operation_id)
        if op is not None:
            completion[op.job_id] = max(completion.get(op.job_id, a.end), a.end)
    return completion


def _need_span(record: OpRecord, phase: str) -> float:
    """Clock seconds a need of `phase` holds its operators (the verifier's `_need_span`)."""
    if phase == "setup":
        return record.setup_end - record.start
    if phase == "run":
        return record.end - record.setup_end
    return record.end - record.start


def _pool_utilization(shop: ShopModel, records: Iterable[OpRecord], end: float) -> dict[str, float]:
    """Held operator-seconds / (size x working seconds of the pool over [now, end])."""
    held = dict.fromkeys(shop.pools, 0.0)
    for r in records:
        for need in shop.operations[r.operation_id].operator_needs:
            held[need.pool_id] += need.quantity * _need_span(r, need.phase)
    utilization: dict[str, float] = {}
    for pool_id, pool in shop.pools.items():
        working = work_seconds(pool.calendar, shop.now, end) if end > shop.now else 0.0
        capacity = pool.size * working
        utilization[pool_id] = held[pool_id] / capacity if capacity > 0 else 0.0
    return utilization


# --- evidence rows across replications (REQ-141) -------------------------------------------


def _percentiles(values: Sequence[float]) -> tuple[float, float, float]:
    p10, p50, p90 = np.percentile(values, PERCENTILES, method="linear")
    return float(p10), float(p50), float(p90)


def _assignment_index(plan: Schedule | None) -> dict[str, Assignment]:
    return {} if plan is None else {a.operation_id: a for a in plan.assignments}


def operation_rows(
    plan_id: str, runs: Sequence[ExecutionRun], plan: Schedule | None
) -> tuple[dict[str, Any], ...]:
    """One evidence row per operation across replications, in the first run's record order.

    Percentiles are linear `numpy.percentile` over replications; `planned_*` are None
    without a plan; `causes` counts every `SLIP_CAUSES` name across replications.
    """
    if not runs:
        return ()
    by_run = [{r.operation_id: r for r in run.records} for run in runs]
    planned = _assignment_index(plan)
    return tuple(
        _operation_row(plan_id, [index[r.operation_id] for index in by_run], planned)
        for r in runs[0].records
    )


def _operation_row(
    plan_id: str, records: Sequence[OpRecord], planned: Mapping[str, Assignment]
) -> dict[str, Any]:
    first = records[0]
    assignment = planned.get(first.operation_id)
    start_p = _percentiles([r.start for r in records])
    end_p = _percentiles([r.end for r in records])
    slips = [slip for r in records if (slip := _start_slip(r)) is not None]
    causes = dict.fromkeys(SLIP_CAUSES, 0)
    for r in records:
        cause = slip_cause(r)
        if cause is not None:
            causes[cause] += 1
    return {
        "row": "operation",
        "plan_id": plan_id,
        "operation_id": first.operation_id,
        "job_id": first.job_id,
        "machine_id": first.machine_id,
        "planned_start": None if assignment is None else assignment.start,
        "planned_end": None if assignment is None else assignment.end,
        "start_p10": start_p[0],
        "start_p50": start_p[1],
        "start_p90": start_p[2],
        "end_p10": end_p[0],
        "end_p50": end_p[1],
        "end_p90": end_p[2],
        "slip_mean": sum(slips) / len(slips) if slips else 0.0,
        "slip_max": max(slips, default=0.0),
        "causes": causes,
        "overrun_seconds_mean": sum(r.overrun_seconds for r in records) / len(records),
        "max_lag_violations": sum(1 for r in records if r.max_lag_violation_seconds > 0),
        "pin_window_misses": sum(1 for r in records if r.pin_window_missed),
        "censored_count": sum(1 for r in records if r.censored),
    }


def job_rows(
    plan_id: str, shop: ShopModel, runs: Sequence[ExecutionRun], plan: Schedule | None
) -> tuple[dict[str, Any], ...]:
    """One evidence row per job across replications, in order of its first record.

    Lateness is signed C_j - d_j in seconds; `due`, lateness percentiles and
    `on_time_share` are None without a due date, `planned_completion` without a plan.
    """
    if not runs:
        return ()
    outcomes = [_JobOutcomes.of(run.records) for run in runs]
    planned = {} if plan is None else planned_completions(shop, plan)
    return tuple(
        _job_row(plan_id, job_id, shop.jobs[job_id].due, outcomes, planned.get(job_id))
        for job_id in outcomes[0].completion
    )


def _job_row(
    plan_id: str,
    job_id: str,
    due: float | None,
    outcomes: Sequence[_JobOutcomes],
    planned_completion: float | None,
) -> dict[str, Any]:
    completions = [o.completion[job_id] for o in outcomes]
    completion_p = _percentiles(completions)
    lateness_p: tuple[float | None, ...] = (None, None, None)
    on_time_share: float | None = None
    if due is not None:
        lateness_p = _percentiles([c - due for c in completions])
        on_time_share = sum(1 for o in outcomes if o.on_time(job_id, due)) / len(outcomes)
    return {
        "row": "job",
        "plan_id": plan_id,
        "job_id": job_id,
        "due": due,
        "planned_completion": planned_completion,
        "completion_p10": completion_p[0],
        "completion_p50": completion_p[1],
        "completion_p90": completion_p[2],
        "lateness_p10": lateness_p[0],
        "lateness_p50": lateness_p[1],
        "lateness_p90": lateness_p[2],
        "on_time_share": on_time_share,
        "censored_count": sum(1 for o in outcomes if job_id in o.censored),
    }


__all__ = [
    "METRIC_NAMES",
    "SLIP_CAUSES",
    "SLIP_TOLERANCE",
    "OpRecord",
    "ReplicationMetrics",
    "busy_seconds",
    "busy_shares",
    "job_rows",
    "machine_occupation",
    "operation_rows",
    "plan_end",
    "plan_occupation",
    "planned_completions",
    "realized_schedule",
    "replication_metrics",
    "slip_cause",
    "top_machine",
    "with_slip_causes",
]
