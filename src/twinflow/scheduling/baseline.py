"""Baseline heuristic: a deterministic list scheduler (COMP-009, REQ-075, REQ-079).

One forward pass over the compiled problem, in three phases:

1. In-progress operations, by id, on their status machine at ``now`` with setup 0 and
   ``end = now + remaining_seconds``.
2. Pinned operations, by (pin earliest_start, id), when every predecessor is already
   placed; a pinned operation still waiting on a predecessor joins phase 3.
3. Every other operation from a ready queue keyed by (predecessor-ready time, id). An
   operation is ready when all its scheduled predecessors are placed.

Each operation tries every option (only the pinned machine when the pin names one) with
``MachineTimeline.find_slot`` on its effective calendar, filtered by operator capacity,
the transfer-lot end bound, ``max_lag`` and ``pin.latest_start``, and keeps the option that
completes first (tie: lowest machine id). Gates follow the SDD Data Model: material gate
g = start (attached) or setup_end (detached); without overlap g >= pred.end + min_lag;
with ``overlap_quantity`` q, g >= pred.setup_end + pred.fixed + q * pred.unit + min_lag
and end >= pred.end + q * succ.unit.

The pass never backtracks, so it cannot prove a problem has no plan: an operation it
cannot place returns UNKNOWN with ``max_lag_unmet`` or ``no_feasible_slot``, and a plan
that ends after ``problem.horizon_end`` returns UNKNOWN with ``horizon_exceeded``, never
INFEASIBLE. Operations of held or done jobs and operations in state ``done`` are not
scheduled and count as finished for precedence. Restart rules are not modeled (TF-003).
The budget is a monotonic deadline checked before each placement: a pass that outlives it
returns UNKNOWN with ``time_limit`` (the pass has no anytime result). One placement scans
the machine's gaps, so a pass is O(n^2) in operations per machine (faster search: TF-003).
"""

from __future__ import annotations

import heapq
import math
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    Job,
    ObjectiveSpec,
    Operation,
    Problem,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
)
from twinflow.scheduling.objectives import evaluate, primary_kind
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.solver import Feature
from twinflow.scheduling.timeline import (
    MachineTimeline,
    PoolNeed,
    PoolTimeline,
    ShiftCrossing,
    Slot,
    SlotRequest,
    effective_calendar,
    hold_operators,
    operator_retry,
)

NAME = "baseline"
CAPABILITIES = frozenset(Feature) - {Feature.RESTART}
_SLACK = 1e-9
"""Float slack on upper bounds (max_lag, latest_start); far below the verifier's 1e-6."""


# --- plain problem values ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Option:
    machine_id: str
    fixed: float
    unit: float
    run: float


@dataclass(frozen=True, slots=True)
class _Link:
    pred_id: str
    min_lag: float
    max_lag: float | None
    overlap: float | None


@dataclass(frozen=True, slots=True)
class _Op:
    """An operation to schedule, as plain values; links name scheduled predecessors only."""

    id: str
    quantity: float
    material_ready: float
    earliest_start: float
    latest_start: float | None
    pinned: bool
    options: tuple[_Option, ...]
    links: tuple[_Link, ...]
    needs: tuple[PoolNeed, ...]
    detached: bool
    shift_crossing: ShiftCrossing
    batch_family: str | None


@dataclass(frozen=True, slots=True)
class _Placed:
    """Where a predecessor ended up, with the chosen option's fixed and unit seconds."""

    setup_end: float
    end: float
    fixed: float
    unit: float

    def transfer(self, quantity: float) -> float:
        """Time the first `quantity` units are done: setup_end + fixed + q * unit."""
        return self.setup_end + self.fixed + quantity * self.unit


@dataclass(frozen=True, slots=True)
class _Gate:
    """Bounds on an operation's material gate from its release and placed predecessors.

    `lots` holds (pred.end, q) per transfer-lot link for the end >= pred.end + q * unit rule.
    """

    ready: float
    limit: float
    lots: tuple[tuple[float, float], ...]


def _options(op: Operation) -> tuple[_Option, ...]:
    pinned_machine = op.pin.machine_id if op.pin else None
    return tuple(
        _Option(
            o.machine_id,
            o.fixed_seconds,
            o.unit_seconds,
            o.fixed_seconds + op.quantity * o.unit_seconds,
        )
        for o in sorted(op.options, key=lambda o: o.machine_id)
        if pinned_machine is None or o.machine_id == pinned_machine
    )


def _plain(op: Operation, job: Job, now: float, scheduled: set[str]) -> _Op:
    pin = op.pin
    earliest = pin.earliest_start if pin and pin.earliest_start is not None else now
    return _Op(
        id=op.id,
        quantity=op.quantity,
        material_ready=max(job.release, op.ready_time),
        earliest_start=max(now, earliest),
        latest_start=pin.latest_start if pin else None,
        pinned=pin is not None,
        options=_options(op),
        links=tuple(
            _Link(link.operation_id, link.min_lag, link.max_lag, link.overlap_quantity)
            for link in op.predecessors
            if link.operation_id in scheduled
        ),
        needs=_needs(op),
        detached=op.setup_detached,
        shift_crossing=op.shift_crossing,
        batch_family=op.batch_family,
    )


def _needs(op: Operation) -> tuple[PoolNeed, ...]:
    return tuple(PoolNeed(n.pool_id, n.quantity, n.phase) for n in op.operator_needs)


def _scheduled_operations(problem: Problem) -> list[tuple[Operation, Job]]:
    """Operations to place: not done, of jobs neither held nor done, in problem order."""
    return [
        (op, job)
        for job in problem.jobs
        if job.status == "open"
        for op in job.operations
        if op.status.state != "done"
    ]


def _issue(code: str, op_id: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=op_id, message=message)


# --- slot filter ---------------------------------------------------------------------------------


class _SlotCheck:
    """`find_slot` filter for one (operation, option): None accepts, else a retry start.

    Upper bounds (pin latest_start, max_lag) only get worse later, so they reject for good
    (inf); `missed_max_lag` records why. The transfer-lot end bound and operator capacity
    ask for a later start.
    """

    __slots__ = ("_end_floor", "_gate", "_op", "_pools", "missed_max_lag")

    def __init__(
        self, op: _Op, gate: _Gate, option: _Option, pools: dict[str, PoolTimeline]
    ) -> None:
        self._op = op
        self._gate = gate
        self._pools = pools
        self._end_floor = max((end + q * option.unit for end, q in gate.lots), default=-math.inf)
        self.missed_max_lag = False

    def __call__(self, slot: Slot) -> float | None:
        latest = self._op.latest_start
        if latest is not None and slot.start > latest + _SLACK:
            return math.inf
        material = slot.setup_end if self._op.detached else slot.start
        if material > self._gate.limit + _SLACK:
            self.missed_max_lag = True
            return math.inf
        retry: float | None = None
        if slot.end < self._end_floor - _SLACK:
            shifted = slot.start + (self._end_floor - slot.end)
            retry = max(shifted, math.nextafter(slot.start, math.inf))
        if self._op.needs:
            blocked = operator_retry(slot, self._op.needs, self._pools)
            if blocked is not None:
                retry = blocked if retry is None else max(retry, blocked)
        return retry


# --- the list scheduler --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Candidate:
    option: _Option
    request: SlotRequest
    slot: Slot


class _ListScheduler:
    """Mutable state of one pass: machine and pool timelines and placed operations."""

    def __init__(self, problem: Problem, deadline: float) -> None:
        self._now = problem.now
        self._deadline = deadline
        calendars = {c.id: c.intervals for c in problem.calendars}
        setups = SetupTable.for_problem(problem)
        self._timelines = {
            m.id: MachineTimeline(
                m.id,
                setups,
                available_from=m.state.available_from,
                batch_capacity=m.batch.capacity if m.batch else None,
            )
            for m in problem.machines
        }
        self._machine_calendar: dict[str, Intervals] = {
            m.id: calendars[m.calendar_id] if m.calendar_id else None for m in problem.machines
        }
        self._pools = {
            p.id: PoolTimeline(p.id, p.size, calendars[p.calendar_id] if p.calendar_id else None)
            for p in problem.operator_pools
        }
        self._calendars: dict[tuple[str, tuple[str, ...]], Intervals] = {}
        self._running: set[str] = set()
        self.placed: dict[str, _Placed] = {}
        self.assignments: list[Assignment] = []

    # --- phases --------------------------------------------------------------------------

    def run(self, problem: Problem) -> ScheduleIssue | None:
        """Place every scheduled operation; the first one that cannot be placed stops the pass."""
        operations = _scheduled_operations(problem)
        scheduled = {op.id for op, _ in operations}
        pending: list[_Op] = []
        for op, job in sorted(operations, key=lambda pair: pair[0].id):
            if op.status.state == "in_progress":
                issue = self._start_in_progress(op)
                if issue is not None:
                    return issue
            else:
                pending.append(_plain(op, job, self._now, scheduled))
        issue = self._place_pins(pending)
        if issue is not None:
            return issue
        return self._drain([op for op in pending if op.id not in self.placed])

    def _place_pins(self, pending: Sequence[_Op]) -> ScheduleIssue | None:
        pins = sorted(
            (op for op in pending if op.pinned), key=lambda op: (op.earliest_start, op.id)
        )
        for op in pins:
            if all(link.pred_id in self.placed for link in op.links):
                issue = self._timed_out() or self._place(op)
                if issue is not None:
                    return issue
        return None

    def _drain(self, ops: Sequence[_Op]) -> ScheduleIssue | None:
        """Kahn's order with a heap keyed by (predecessor-ready time, op id)."""
        by_id = {op.id: op for op in ops}
        waiting: dict[str, int] = {}
        successors: dict[str, list[str]] = defaultdict(list)
        for op in ops:
            unplaced = [link.pred_id for link in op.links if link.pred_id not in self.placed]
            waiting[op.id] = len(unplaced)
            for pred_id in unplaced:
                successors[pred_id].append(op.id)
        queue = [(self._gate(op).ready, op.id) for op in ops if waiting[op.id] == 0]
        heapq.heapify(queue)
        while queue:
            _, op_id = heapq.heappop(queue)
            issue = self._timed_out() or self._place(by_id[op_id])
            if issue is not None:
                return issue
            for succ_id in successors[op_id]:
                waiting[succ_id] -= 1
                if waiting[succ_id] == 0:
                    heapq.heappush(queue, (self._gate(by_id[succ_id]).ready, succ_id))
        return None

    def _timed_out(self) -> ScheduleIssue | None:
        """`time_limit` once the monotonic deadline has passed, else None."""
        if time.monotonic() < self._deadline:
            return None
        message = "the baseline pass did not finish within the time budget"
        return ScheduleIssue(code="time_limit", path="budget_seconds", message=message)

    # --- in-progress ---------------------------------------------------------------------

    def _start_in_progress(self, op: Operation) -> ScheduleIssue | None:
        """Place an in-progress op on its status machine on [now, now + remaining)."""
        machine_id = op.status.machine_id or ""
        remaining = op.status.remaining_seconds or 0.0
        slot = Slot(
            start=self._now, setup_end=self._now, end=self._now + remaining, setup_seconds=0.0
        )
        needs = _needs(op)
        if machine_id in self._running:
            return _issue("no_feasible_slot", op.id, f"{op.id}: {machine_id} already runs an op")
        if needs and operator_retry(slot, needs, self._pools) is not None:
            return _issue("no_feasible_slot", op.id, f"{op.id}: operators busy at now")
        request = SlotRequest(
            op_id=op.id,
            run_seconds=remaining,
            earliest_start=self._now,
            gate_ready=self._now,
            quantity=op.quantity,
        )
        option = next(o for o in _options(op) if o.machine_id == machine_id)
        self._running.add(machine_id)
        self._commit(op.id, needs, _Candidate(option, request, slot))
        return None

    # --- one pending operation -----------------------------------------------------------

    def _gate(self, op: _Op) -> _Gate:
        ready, limit = op.material_ready, math.inf
        lots: list[tuple[float, float]] = []
        for link in op.links:
            pred = self.placed[link.pred_id]
            base = pred.end if link.overlap is None else pred.transfer(link.overlap)
            ready = max(ready, base + link.min_lag)
            if link.max_lag is not None:
                limit = min(limit, base + link.max_lag)
            if link.overlap is not None:
                lots.append((pred.end, link.overlap))
        return _Gate(ready, limit, tuple(lots))

    def _place(self, op: _Op) -> ScheduleIssue | None:
        """Place `op` on the option that completes first; an issue when none can."""
        gate = self._gate(op)
        best: _Candidate | None = None
        missed_max_lag = False
        for option in op.options:
            request = self._request(op, option, gate)
            check = _SlotCheck(op, gate, option, self._pools)
            slot = self._timelines[option.machine_id].find_slot(request, check)
            missed_max_lag |= check.missed_max_lag
            if slot is not None and (best is None or slot.end < best.slot.end):
                best = _Candidate(option, request, slot)
        if best is None:
            return _unplaceable(op.id, missed_max_lag)
        self._commit(op.id, op.needs, best)
        return None

    def _request(self, op: _Op, option: _Option, gate: _Gate) -> SlotRequest:
        return SlotRequest(
            op_id=op.id,
            run_seconds=option.run,
            earliest_start=op.earliest_start,
            gate_ready=gate.ready,
            quantity=op.quantity,
            calendar=self._calendar(option.machine_id, op.needs),
            detached=op.detached,
            shift_crossing=op.shift_crossing,
            batch_family=op.batch_family,
        )

    def _calendar(self, machine_id: str, needs: Iterable[PoolNeed]) -> Intervals:
        """Effective calendar: machine calendar intersected with each needed pool's (memoized)."""
        key = (machine_id, tuple(need.pool_id for need in needs))
        if key not in self._calendars:
            pool_calendars = (self._pools[pool_id].calendar for pool_id in key[1])
            self._calendars[key] = effective_calendar(
                self._machine_calendar[machine_id], pool_calendars
            )
        return self._calendars[key]

    def _commit(self, op_id: str, needs: Sequence[PoolNeed], chosen: _Candidate) -> None:
        machine_id, slot = chosen.option.machine_id, chosen.slot
        batch_id = self._timelines[machine_id].place(chosen.request, slot)
        if needs:
            hold_operators(slot, needs, self._pools)
        self.placed[op_id] = _Placed(
            slot.setup_end, slot.end, chosen.option.fixed, chosen.option.unit
        )
        self.assignments.append(
            Assignment(
                operation_id=op_id,
                machine_id=machine_id,
                start=slot.start,
                setup_end=slot.setup_end,
                end=slot.end,
                batch_id=batch_id,
            )
        )


def _unplaceable(op_id: str, missed_max_lag: bool) -> ScheduleIssue:
    if missed_max_lag:
        message = f"{op_id}: no slot meets its max_lag after the predecessors already placed"
        return _issue("max_lag_unmet", op_id, message)
    message = f"{op_id}: no slot on any option within calendars, pins and operators"
    return _issue("no_feasible_slot", op_id, message)


def _horizon_issue(problem: Problem, assignments: Sequence[Assignment]) -> ScheduleIssue | None:
    """`horizon_exceeded` for the first operation that ends after `horizon_end`."""
    horizon = problem.horizon_end
    if horizon is None:
        return None
    late = next((a for a in assignments if a.end > horizon + _SLACK), None)
    if late is None:
        return None
    message = f"{late.operation_id}: ends at {late.end:g}, after horizon_end {horizon:g}"
    return _issue("horizon_exceeded", late.operation_id, message)


# --- the solver ----------------------------------------------------------------------------------


class BaselineSolver:
    """The default solver: one deterministic list-scheduling pass (COMP-009).

    Returns FEASIBLE with `primary_value` set to the primary objective of `objective`, or
    UNKNOWN with no assignments and one named issue; never INFEASIBLE. The primary value
    is evaluated with default class weights because compiled jobs carry their effective
    weight in `weight_override` (D-066).
    """

    @property
    def name(self) -> str:
        """Solver id reported in every schedule."""
        return NAME

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature except `restart` (warm-up after idle is TF-003)."""
        return CAPABILITIES

    def solve(
        self, problem: Problem, *, objective: ObjectiveSpec, budget_seconds: float
    ) -> Schedule:
        """One pass over `problem`, cut off with `time_limit` after `budget_seconds`."""
        started = time.perf_counter()
        scheduler = _ListScheduler(problem, time.monotonic() + budget_seconds)
        issue = scheduler.run(problem) or _horizon_issue(problem, scheduler.assignments)
        if issue is not None:
            elapsed = time.perf_counter() - started
            return Schedule(
                status="UNKNOWN",
                assignments=(),
                solver=NAME,
                elapsed_seconds=elapsed,
                issues=(issue,),
                budget_enforced=True,
            )
        schedule = Schedule(
            status="FEASIBLE",
            assignments=tuple(scheduler.assignments),
            solver=NAME,
            elapsed_seconds=0.0,
            budget_enforced=True,
        )
        compiled = CompiledProblem(problem=problem, profile=SchedulingProfile(objective=objective))
        value = evaluate(compiled, schedule)[primary_kind(objective)]
        elapsed = time.perf_counter() - started
        return schedule.model_copy(update={"primary_value": value, "elapsed_seconds": elapsed})
