"""Shop model: the compiled v2 problem resolved for execution (COMP-134, REQ-127, D-085).

`build_shop` turns a `CompiledProblem` into frozen, picklable records with id-keyed index
maps, built once per evaluation and sent once to each worker. It resolves values only;
every scheduling rule comes from `twinflow.scheduling` (setup functions through
`SetupTable`, calendar intersection through `calendar_math`). No file or network I/O.

`censor_time` gives the run bound T_c (D-107); it takes the planned makespan because the
shop is built without a plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from twinflow.scheduling import calendar_math
from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import (
    BatchCapacity,
    CompiledProblem,
    Interval,
    Job,
    Machine,
    MachineState,
    Operation,
    OperationStatus,
    OperatorNeed,
    Pin,
    Precedence,
    ProcessingOption,
    RestartRule,
    SetupFunction,
    SetupParams,
)
from twinflow.scheduling.setups import SetupTable

DAY_SECONDS = 86_400.0
"""Seconds in a day (the D-107 floor is 30 days)."""

CalendarIntervals = tuple[Interval, ...] | None
"""A calendar's working intervals, or None for always working."""

CalendarKey = tuple[str | None, ...]
"""Memo key of an effective calendar: (machine calendar id, *needed pool calendar ids)."""


@dataclass(frozen=True)
class ShopMachine:
    """A machine with its resolved setup function (machine first, then own center; D-064)."""

    id: str
    work_center_id: str
    calendar_id: str | None
    calendar: CalendarIntervals
    setup_function: SetupFunction | None
    batch_capacity: BatchCapacity | None
    restart: RestartRule | None
    state: MachineState


@dataclass(frozen=True)
class ShopPool:
    """An operator pool: `size` operators working on `calendar`."""

    id: str
    size: int
    calendar_id: str | None
    calendar: CalendarIntervals


@dataclass(frozen=True)
class ShopJob:
    """A non-held job with its effective weight (`weight_override`, else class weight)."""

    id: str
    weight: float
    release: float
    due: float | None


@dataclass(frozen=True)
class ShopOperation:
    """A pending operation (not done, job not held or done); fields are the model values."""

    id: str
    job_id: str
    work_center_id: str
    quantity: float
    options: tuple[ProcessingOption, ...]
    predecessors: tuple[Precedence, ...]
    ready_time: float
    setup_params: SetupParams
    setup_family: str | None
    batch_family: str | None
    detached: bool
    operator_needs: tuple[OperatorNeed, ...]
    shift_crossing: str
    pin: Pin | None
    status: OperationStatus


@dataclass(frozen=True)
class ShopModel:
    """Everything the executor reads, keyed by id, in the problem's document order.

    `successors` and `job_operations` index pending operations only. `setups` and the
    effective-calendar memo are excluded from equality so two builds compare equal.
    """

    problem_id: str
    now: float
    horizon_end: float | None
    machines: Mapping[str, ShopMachine]
    pools: Mapping[str, ShopPool]
    jobs: Mapping[str, ShopJob]
    operations: Mapping[str, ShopOperation]
    held_job_ids: frozenset[str]
    done_operation_ids: frozenset[str]
    calendar_end: float | None
    successors: Mapping[str, tuple[str, ...]]
    job_operations: Mapping[str, tuple[str, ...]]
    setups: SetupTable = field(compare=False, repr=False)
    _calendar_memo: dict[CalendarKey, Intervals] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def calendar_bounded(self) -> bool:
        """True when every option of every pending op has a calendar (none always works)."""
        return all(
            self.effective_calendar(option.machine_id, op.id) is not None
            for op in self.operations.values()
            for option in op.options
        )

    def effective_calendar(self, machine_id: str, operation_id: str) -> Intervals:
        """Machine calendar intersected with each needed pool's calendar, in need order.

        Memoized by the calendar-id tuple: equal tuples return the same object.
        """
        machine = self.machines[machine_id]
        pools = [self.pools[need.pool_id] for need in self.operations[operation_id].operator_needs]
        key: CalendarKey = (machine.calendar_id, *(pool.calendar_id for pool in pools))
        if key not in self._calendar_memo:
            result: Intervals = machine.calendar
            for pool in pools:
                result = calendar_math.intersect(result, pool.calendar)
            self._calendar_memo[key] = None if result is None else tuple(result)
        return self._calendar_memo[key]


def censor_time(shop: ShopModel, planned_makespan: float) -> float:
    """T_c: when the run stops (D-107).

    `planned_makespan` is planned last end minus `now`, in seconds. When every option of
    every pending op has an effective calendar, nothing works after `calendar_end`.
    Otherwise some op can run on an always-working machine and crew, bounded only by
    `now + max(10 x planned_makespan, 30 days)`, and never earlier than `calendar_end`.
    """
    if shop.calendar_end is not None and shop.calendar_bounded:
        return shop.calendar_end
    floor = shop.now + max(10.0 * planned_makespan, 30.0 * DAY_SECONDS)
    return floor if shop.calendar_end is None else max(shop.calendar_end, floor)


def build_shop(compiled: CompiledProblem) -> ShopModel:
    """Resolve the compiled problem into a `ShopModel`; pure and deterministic."""
    problem = compiled.problem
    setups = SetupTable.for_problem(problem)
    calendars = {calendar.id: tuple(calendar.intervals) for calendar in problem.calendars}
    held = frozenset(compiled.held_job_ids) | {j.id for j in problem.jobs if j.status == "held"}
    active = [job for job in problem.jobs if job.id not in held]
    pending = [
        (job, op)
        for job in active
        if job.status != "done"
        for op in job.operations
        if op.status.state != "done"
    ]
    machines = {m.id: _machine(m, calendars, setups) for m in problem.machines}
    pools = {
        pool.id: ShopPool(
            pool.id, pool.size, pool.calendar_id, _calendar(pool.calendar_id, calendars)
        )
        for pool in problem.operator_pools
    }
    operations = {op.id: _operation(job.id, op) for job, op in pending}
    return ShopModel(
        problem_id=problem.id,
        now=problem.now,
        horizon_end=problem.horizon_end,
        machines=machines,
        pools=pools,
        jobs={job.id: _job(job, compiled) for job in active},
        operations=operations,
        held_job_ids=held,
        done_operation_ids=_done_operation_ids(active),
        calendar_end=_calendar_end([*machines.values(), *pools.values()]),
        successors=_successors(operations),
        job_operations=_job_operations(operations),
        setups=setups,
    )


def _calendar(
    calendar_id: str | None, calendars: Mapping[str, tuple[Interval, ...]]
) -> CalendarIntervals:
    return None if calendar_id is None else calendars[calendar_id]


def _machine(
    machine: Machine, calendars: Mapping[str, tuple[Interval, ...]], setups: SetupTable
) -> ShopMachine:
    return ShopMachine(
        id=machine.id,
        work_center_id=machine.work_center_id,
        calendar_id=machine.calendar_id,
        calendar=_calendar(machine.calendar_id, calendars),
        setup_function=setups.function_for(machine.id),
        batch_capacity=machine.batch,
        restart=machine.restart,
        state=machine.state,
    )


def _job(job: Job, compiled: CompiledProblem) -> ShopJob:
    weight = job.weight_override or compiled.profile.class_weights[job.weight_class]
    return ShopJob(id=job.id, weight=weight, release=job.release, due=job.due)


def _operation(job_id: str, op: Operation) -> ShopOperation:
    return ShopOperation(
        id=op.id,
        job_id=job_id,
        work_center_id=op.work_center_id,
        quantity=op.quantity,
        options=op.options,
        predecessors=op.predecessors,
        ready_time=op.ready_time,
        setup_params=op.setup_params,
        setup_family=op.setup_family,
        batch_family=op.batch_family,
        detached=op.setup_detached,
        operator_needs=op.operator_needs,
        shift_crossing=op.shift_crossing,
        pin=op.pin,
        status=op.status,
    )


def _done_operation_ids(active: Iterable[Job]) -> frozenset[str]:
    """Ops with status `done` plus every op of a `done` job (for predecessor checks)."""
    return frozenset(
        op.id
        for job in active
        for op in job.operations
        if job.status == "done" or op.status.state == "done"
    )


def _calendar_end(resources: Iterable[ShopMachine | ShopPool]) -> float | None:
    """Latest working-interval end over the calendars machines and pools use (D-107)."""
    ends = [r.calendar[-1][1] for r in resources if r.calendar]
    return max(ends) if ends else None


def _successors(operations: Mapping[str, ShopOperation]) -> dict[str, tuple[str, ...]]:
    """Pending successors of every pending op, in document order."""
    found: dict[str, list[str]] = {op_id: [] for op_id in operations}
    for op in operations.values():
        for link in op.predecessors:
            if link.operation_id in found:
                found[link.operation_id].append(op.id)
    return {op_id: tuple(ids) for op_id, ids in found.items()}


def _job_operations(operations: Mapping[str, ShopOperation]) -> dict[str, tuple[str, ...]]:
    """Pending op ids of every job that has one, in document order."""
    found: dict[str, list[str]] = {}
    for op in operations.values():
        found.setdefault(op.job_id, []).append(op.id)
    return {job_id: tuple(ids) for job_id, ids in found.items()}


__all__ = [
    "DAY_SECONDS",
    "ShopJob",
    "ShopMachine",
    "ShopModel",
    "ShopOperation",
    "ShopPool",
    "build_shop",
    "censor_time",
]
