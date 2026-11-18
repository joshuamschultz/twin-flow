"""Problem index: the compiled problem as plain values, built once in O(n) (COMP-003, REQ-093).

`ProblemIndex.build(problem, phi)` is the shared base of the decoder, the disjunctive
graph, the dispatch engine, ATCS, shifting bottleneck and local search (SDD Architecture,
Data Model "ProblemIndex"). It reads the Pydantic problem once and creates no Pydantic
object; everything it holds is a frozen slotted dataclass, tuple or dict.

Ops are the *scheduled* ones only: not `done`, of `open` jobs, in problem order. Each op
has a dense int position (`pos[op_id]`, the index into `ops`) so hot loops can work on ints;
the string-keyed methods are the readable face. Per op:

- `release` = max(job.release, op.ready_time), the material release;
- `options`: the eligible machines in machine id order, run = fixed + quantity * unit; an
  in-progress op keeps only its status machine, else a pin naming a machine keeps only it;
- `predecessors`: links to scheduled ops only (a done predecessor is already satisfied);
- `remaining_shortest_run` (R_after): sum over the job's later scheduled ops of their
  shortest eligible run; `local_due` (dloc) = due - phi * R_after, None without a due.

Job weight is the effective one: `weight_override` (compiled problems always carry it,
D-066), else the default class weight. Machines carry their decoder inputs (availability,
calendar intervals, batch capacity, restart rule, last setup state) and their hour costs;
`setups` is the problem's memoized `SetupTable`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from twinflow.scheduling.model import (
    Interval,
    Job,
    Machine,
    Operation,
    Problem,
    RestartRule,
    SetupParams,
)
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.timeline import PoolNeed, ShiftCrossing

DEFAULT_PHI = 2.0
"""Local-due factor: a job spends about twice its processing time per stage (Pinedo p.446)."""

DEFAULT_CLASS_WEIGHTS: Mapping[int, float] = {1: 1.0, 2: 2.0, 4: 4.0}
"""Class weights used when a job has no `weight_override` (an uncompiled problem)."""

Calendar = tuple[Interval, ...] | None
"""Working intervals `[start, end)`; None means always working."""


@dataclass(frozen=True, slots=True)
class OpOption:
    """One eligible machine of an op: run = fixed + quantity * unit seconds."""

    machine_id: str
    run: float
    preference_cost: float
    fixed_seconds: float
    unit_seconds: float


@dataclass(frozen=True, slots=True)
class Link:
    """A precedence link from scheduled op `pred_id` (position `pred_pos`)."""

    pred_id: str
    pred_pos: int
    min_lag: float
    max_lag: float | None
    overlap: float | None


@dataclass(frozen=True, slots=True)
class PinWindow:
    """A pin as plain values: machine and/or a window for the start."""

    machine_id: str | None
    earliest_start: float | None
    latest_start: float | None


@dataclass(frozen=True, slots=True)
class InProgress:
    """An op already running at `now`: its machine and remaining seconds."""

    machine_id: str | None
    remaining_seconds: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OpData:
    """One scheduled op as plain values (see the module docstring)."""

    id: str
    job_pos: int
    route_pos: int
    work_center_id: str
    quantity: float
    release: float
    options: tuple[OpOption, ...]
    predecessors: tuple[Link, ...]
    setup_detached: bool
    shift_crossing: ShiftCrossing
    batch_family: str | None
    operator_needs: tuple[PoolNeed, ...]
    pin: PinWindow | None
    in_progress: InProgress | None
    remaining_shortest_run: float
    local_due: float | None


@dataclass(frozen=True, slots=True)
class JobData:
    """A job with scheduled ops: effective weight, due and its op positions in route order."""

    id: str
    weight: float
    due: float | None
    ops: tuple[int, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class MachineData:
    """A machine's decoder and cost inputs as plain values (restart rule: the frozen model)."""

    id: str
    work_center_id: str
    available_from: float
    calendar: Calendar
    batch_capacity: float | None
    restart: RestartRule | None
    last_setup_params: SetupParams
    last_setup_family: str | None
    hour_cost: float
    setup_hour_cost: float


@dataclass(frozen=True, slots=True)
class PoolData:
    """An operator pool: size and calendar."""

    id: str
    size: int
    calendar: Calendar


@dataclass(frozen=True, slots=True, kw_only=True)
class ProblemIndex:
    """Plain, position-indexed view of a compiled problem (see the module docstring)."""

    now: float
    horizon_end: float | None
    phi: float
    op_ids: tuple[str, ...]
    pos: Mapping[str, int]
    ops: tuple[OpData, ...]
    successors: tuple[tuple[int, ...], ...]
    jobs: tuple[JobData, ...]
    job_pos: Mapping[str, int]
    machines: tuple[MachineData, ...]
    machine_pos: Mapping[str, int]
    center_machines: Mapping[str, tuple[str, ...]]
    pools: Mapping[str, PoolData]
    setups: SetupTable

    @classmethod
    def build(cls, problem: Problem, phi: float = DEFAULT_PHI) -> ProblemIndex:
        """Index `problem` in one pass over its jobs, machines and pools."""
        scheduled = _scheduled_jobs(problem)
        pos = {operation.id: p for p, (_, operation) in enumerate(_flatten(scheduled))}
        ops_data: list[OpData] = []
        jobs: list[JobData] = []
        for job_pos, (job, ops) in enumerate(scheduled):
            first = len(ops_data)
            ops_data.extend(_job_ops(job, ops, job_pos, pos, phi))
            positions = tuple(range(first, len(ops_data)))
            jobs.append(JobData(job.id, _weight(job), job.due, positions))
        calendars = {c.id: c.intervals for c in problem.calendars}
        machines = tuple(_machine_data(m, calendars) for m in problem.machines)
        return cls(
            now=problem.now,
            horizon_end=problem.horizon_end,
            phi=phi,
            op_ids=tuple(pos),
            pos=pos,
            ops=tuple(ops_data),
            successors=_successors(ops_data),
            jobs=tuple(jobs),
            job_pos={job.id: p for p, job in enumerate(jobs)},
            machines=machines,
            machine_pos={m.id: p for p, m in enumerate(machines)},
            center_machines=_center_machines(problem, machines),
            pools={
                pool.id: PoolData(pool.id, pool.size, _calendar(pool.calendar_id, calendars))
                for pool in problem.operator_pools
            },
            setups=SetupTable.for_problem(problem),
        )

    def op(self, op_id: str) -> OpData:
        return self.ops[self.pos[op_id]]

    def job_of(self, op_id: str) -> str:
        return self.jobs[self.op(op_id).job_pos].id

    def release(self, op_id: str) -> float:
        return self.op(op_id).release

    def work_center(self, op_id: str) -> str:
        return self.op(op_id).work_center_id

    def options(self, op_id: str) -> tuple[OpOption, ...]:
        return self.op(op_id).options

    def predecessors(self, op_id: str) -> tuple[Link, ...]:
        return self.op(op_id).predecessors

    def remaining_shortest_run(self, op_id: str) -> float:
        return self.op(op_id).remaining_shortest_run

    def local_due(self, op_id: str) -> float | None:
        return self.op(op_id).local_due

    def weight(self, job_id: str) -> float:
        return self.jobs[self.job_pos[job_id]].weight

    def due(self, job_id: str) -> float | None:
        return self.jobs[self.job_pos[job_id]].due

    def machines_of(self, center_id: str) -> tuple[str, ...]:
        """Machine ids of a work center in problem order; () for an unknown center."""
        return self.center_machines.get(center_id, ())


# --- build helpers (each reads Pydantic values, creates plain ones) -------------------------

_ScheduledJob = tuple[Job, tuple[Operation, ...]]


def _scheduled_jobs(problem: Problem) -> list[_ScheduledJob]:
    """Open jobs with their not-done ops, both in problem order; jobs with none left out."""
    scheduled: list[_ScheduledJob] = []
    for job in problem.jobs:
        if job.status != "open":
            continue
        ops = tuple(o for o in job.operations if o.status.state != "done")
        if ops:
            scheduled.append((job, ops))
    return scheduled


def _flatten(scheduled: list[_ScheduledJob]) -> list[tuple[Job, Operation]]:
    return [(job, operation) for job, ops in scheduled for operation in ops]


def _weight(job: Job) -> float:
    if job.weight_override is not None:
        return job.weight_override
    return DEFAULT_CLASS_WEIGHTS[job.weight_class]


def _eligible_options(operation: Operation) -> tuple[OpOption, ...]:
    """Options in machine id order; the status machine (in progress) or pin machine only."""
    only: str | None = None
    if operation.status.state == "in_progress":
        only = operation.status.machine_id
    elif operation.pin is not None:
        only = operation.pin.machine_id
    options = tuple(
        OpOption(
            o.machine_id,
            o.fixed_seconds + operation.quantity * o.unit_seconds,
            o.preference_cost,
            o.fixed_seconds,
            o.unit_seconds,
        )
        for o in sorted(operation.options, key=lambda o: o.machine_id)
        if only is None or o.machine_id == only
    )
    if not options:
        raise ValueError(f"operation {operation.id} has no eligible machine option")
    return options


def _job_ops(
    job: Job, ops: tuple[Operation, ...], job_pos: int, pos: Mapping[str, int], phi: float
) -> list[OpData]:
    """A job's ops as `OpData`; R_after is a reverse running sum of shortest runs."""
    options = [_eligible_options(o) for o in ops]
    remaining = [0.0] * len(ops)
    for k in range(len(ops) - 2, -1, -1):
        remaining[k] = remaining[k + 1] + min(opt.run for opt in options[k + 1])
    route = {o.id: k for k, o in enumerate(job.operations)}
    return [
        _op_data(o, options[k], remaining[k], job, job_pos, route[o.id], pos, phi)
        for k, o in enumerate(ops)
    ]


def _op_data(
    operation: Operation,
    options: tuple[OpOption, ...],
    remaining: float,
    job: Job,
    job_pos: int,
    route_pos: int,
    pos: Mapping[str, int],
    phi: float,
) -> OpData:
    status = operation.status
    pin = operation.pin
    return OpData(
        id=operation.id,
        job_pos=job_pos,
        route_pos=route_pos,
        work_center_id=operation.work_center_id,
        quantity=operation.quantity,
        release=max(job.release, operation.ready_time),
        options=options,
        predecessors=tuple(
            Link(lk.operation_id, pos[lk.operation_id], lk.min_lag, lk.max_lag, lk.overlap_quantity)
            for lk in operation.predecessors
            if lk.operation_id in pos
        ),
        setup_detached=operation.setup_detached,
        shift_crossing=operation.shift_crossing,
        batch_family=operation.batch_family,
        operator_needs=tuple(
            PoolNeed(need.pool_id, need.quantity, need.phase) for need in operation.operator_needs
        ),
        pin=None
        if pin is None
        else PinWindow(pin.machine_id, pin.earliest_start, pin.latest_start),
        in_progress=(
            InProgress(status.machine_id, status.remaining_seconds)
            if status.state == "in_progress"
            else None
        ),
        remaining_shortest_run=remaining,
        local_due=None if job.due is None else job.due - phi * remaining,
    )


def _successors(ops: list[OpData]) -> tuple[tuple[int, ...], ...]:
    """Per op position, the positions of the scheduled ops that link to it."""
    successors: list[list[int]] = [[] for _ in ops]
    for p, op_data in enumerate(ops):
        for link in op_data.predecessors:
            successors[link.pred_pos].append(p)
    return tuple(tuple(s) for s in successors)


def _calendar(calendar_id: str | None, calendars: Mapping[str, tuple[Interval, ...]]) -> Calendar:
    return None if calendar_id is None else calendars[calendar_id]


def _machine_data(machine: Machine, calendars: Mapping[str, tuple[Interval, ...]]) -> MachineData:
    state = machine.state
    return MachineData(
        id=machine.id,
        work_center_id=machine.work_center_id,
        available_from=state.available_from,
        calendar=_calendar(machine.calendar_id, calendars),
        batch_capacity=None if machine.batch is None else machine.batch.capacity,
        restart=machine.restart,
        last_setup_params=state.last_setup_params,
        last_setup_family=state.last_setup_family,
        hour_cost=machine.hour_cost,
        setup_hour_cost=machine.setup_hour_cost,
    )


def _center_machines(
    problem: Problem, machines: tuple[MachineData, ...]
) -> dict[str, tuple[str, ...]]:
    """Machine ids per work center in problem order (every center, possibly empty)."""
    by_center: dict[str, list[str]] = {center.id: [] for center in problem.work_centers}
    for m in machines:
        by_center.setdefault(m.work_center_id, []).append(m.id)
    return {center: tuple(ids) for center, ids in by_center.items()}
