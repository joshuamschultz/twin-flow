"""The shop floor of one run: machine processes and the shared unit path (COMP-135).

One SimPy process per machine asks the `Sequencer` for its next unit, then runs it through
`start_unit`, the one path plan and dispatch control share (D-088, D-102):

1. earliest start: planned start (an earliest start only) and the machine free;
2. attached members' material gates (release, ready time, predecessor links);
3. machine up, calendar rule on expected durations (D-089), operators by priority; a grant
   that arrives late is re-checked against the machine and the calendar;
4. setup work (setup function on the actual predecessor + D-074 warm-up, times its factor);
5. operator swap at the setup end, then detached members' gates (detached setup);
6. run work, emitting transfer-lot progress for overlap successors (D-091);
7. the last-lot bound of overlap links, release, completion.

Work is preempt-resume (D-094): a segment ends at the earlier of its end and the machine
going down; the remaining work resumes when it is up. `pause` work consumes working
seconds of the effective calendar through `calendar_math` only. The rules are the
verifier's rules; the floor adds no scheduling math.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field

import simpy

from twinflow.engine.rng import SOURCE_RUN_FACTOR, SOURCE_SETUP_FACTOR, keyed_uniform
from twinflow.execution._resources import MachineRuntime, OperatorPool
from twinflow.execution._units import PoolNeeds, Sequencer, Unit
from twinflow.execution.distributions import quantile
from twinflow.execution.metrics import OpRecord
from twinflow.execution.shop import ShopModel, ShopOperation
from twinflow.execution.sources import DistributionSource, OpContext
from twinflow.scheduling.calendar_math import (
    Intervals,
    finish_after_work,
    fits_within,
    next_working,
    work_seconds,
)
from twinflow.scheduling.model import Precedence
from twinflow.scheduling.verifier import TOLERANCE

Steps = Generator[simpy.Event, object, None]
"""A SimPy process body (or a part of one used with `yield from`)."""


@dataclass(frozen=True)
class RepDraws:
    """Keyed draws of one replication (D-095): every factor is a function of
    (seed, replication, source, entity id) only, so plans pair without joint runs."""

    seed: int
    replication: int
    source: DistributionSource

    def run_factor(self, ctx: OpContext, key: str) -> float:
        """Actual-over-planned run seconds of the unit keyed by `key`."""
        u = keyed_uniform(self.seed, self.replication, SOURCE_RUN_FACTOR, key)
        return quantile(self.source.run_factor(ctx), u)

    def setup_factor(self, ctx: OpContext, key: str) -> float:
        """Actual-over-planned setup seconds of the unit keyed by `key`."""
        u = keyed_uniform(self.seed, self.replication, SOURCE_SETUP_FACTOR, key)
        return quantile(self.source.setup_factor(ctx), u)


class _Gate:
    """An operation's material gate: opens when every predecessor link is met.

    `time` is the latest requirement so far (release, ready time, link end plus min lag);
    `limits` are the latest gate times the max lags allow (recorded, D-090).
    """

    __slots__ = ("event", "limits", "pending", "time")

    def __init__(self, time: float) -> None:
        self.time = time
        self.pending = 0
        self.event: simpy.Event | None = None
        self.limits: list[float] = []

    def satisfy(self, ready_at: float, limit: float | None) -> bool:
        """One link is met: the gate may open at `ready_at` or later. True when it was the
        last one (the gate is open from `time`)."""
        self.time = max(self.time, ready_at)
        if limit is not None:
            self.limits.append(limit)
        self.pending -= 1
        if self.pending == 0 and self.event is not None:
            self.event.succeed()
        return self.pending == 0


@dataclass(slots=True)
class _UnitRun:
    """Mutable progress of one unit; turned into `OpRecord`s at the end."""

    unit: Unit
    machine_free: float
    start: float | None = None
    setup_end: float | None = None
    expected_setup: float = 0.0
    setup_seconds: float = 0.0
    run_seconds: float = 0.0
    run_factor: float = 1.0
    interrupted: float = 0.0
    setup_held: bool = False
    both_held: bool = False
    run_held: bool = False
    operator_time: float | None = None
    calendar_time: float | None = None
    machine_up_time: float | None = None


@dataclass(frozen=True, slots=True)
class _Milestone:
    """Transfer-lot progress: when run work reaches `threshold`, `succ`'s link is met."""

    threshold: float
    succ_id: str
    link: Precedence


@dataclass
class Floor:
    """Everything one run shares: machines, pools, gates and per-operation results."""

    env: simpy.Environment
    shop: ShopModel
    draws: RepDraws
    sequencer: Sequencer
    machines: dict[str, MachineRuntime] = field(init=False)
    pools: dict[str, OperatorPool] = field(init=False)
    gates: dict[str, _Gate] = field(init=False)
    successors: dict[str, list[tuple[str, Precedence]]] = field(init=False)
    ends: dict[str, float] = field(init=False, default_factory=dict)
    runs: dict[str, _UnitRun] = field(init=False, default_factory=dict)
    lag_violation: dict[str, float] = field(init=False, default_factory=dict)
    remaining: int = field(init=False)
    _finished: dict[str, simpy.Event] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        now = self.shop.now
        self.machines = {
            m.id: MachineRuntime(self.env, m.id, max(now, m.state.available_from))
            for m in self.shop.machines.values()
        }
        self.pools = {p.id: OperatorPool(self.env, p.size) for p in self.shop.pools.values()}
        self.successors = {op_id: [] for op_id in self.shop.operations}
        self.gates = {op.id: self._gate(op) for op in self.shop.operations.values()}
        self.remaining = len(self.shop.operations)

    def _gate(self, op: ShopOperation) -> _Gate:
        """Release and ready time now; a done predecessor counts as finished at now; a
        predecessor that is neither pending nor done (held) never finishes."""
        gate = _Gate(max(self.shop.jobs[op.job_id].release, op.ready_time))
        for link in op.predecessors:
            if link.operation_id in self.successors:
                self.successors[link.operation_id].append((op.id, link))
                gate.pending += 1
            elif link.operation_id in self.shop.done_operation_ids:
                gate.time = max(gate.time, self.shop.now)
            else:
                gate.pending += 1
        return gate

    def start(self) -> None:
        """Report the gates open from the start, then start one process per machine the
        sequencer names."""
        for op_id, gate in self.gates.items():
            if gate.pending == 0:
                self.sequencer.gate_open(op_id, gate.time)
        for machine_id in self.sequencer.machine_ids():
            self.env.process(self._machine_process(self.machines[machine_id]))

    def _machine_process(self, machine: MachineRuntime) -> Steps:
        while True:
            nxt = self.sequencer.next_unit(machine)
            if nxt is None:
                return
            if isinstance(nxt, simpy.Event):
                yield nxt
                continue
            yield from self.start_unit(machine, nxt)

    # --- the shared unit path -----------------------------------------------------------------

    def start_unit(self, machine: MachineRuntime, unit: Unit) -> Steps:
        """Run one unit from its earliest start to its end (shared by every control)."""
        run = _UnitRun(unit, self.env.now if unit.in_progress else machine.last_end)
        for op in unit.members:
            self.runs[op.id] = run
        yield from self._await_start(machine, run)
        self._draw(run)
        yield from self._work(machine, run, run.setup_seconds, ())
        run.setup_end = self.env.now
        yield from self._swap_operators(run)
        yield from self._await_gates(unit.detached)
        self._record_lags(unit.detached)
        yield from self._work(machine, run, run.run_seconds, self._milestones(run))
        yield from self._last_lot(run)
        self._release(unit.run_needs if run.run_held else ())
        self._release(unit.both_needs if run.both_held else ())
        self._finish(machine, run)

    def _await_start(self, machine: MachineRuntime, run: _UnitRun) -> Steps:
        unit = run.unit
        planned = self.env.now if unit.planned_start is None else unit.planned_start
        earliest = self.env.now if unit.in_progress else max(planned, run.machine_free)
        yield from self._wait_until(earliest)
        yield from self._await_gates(unit.attached)
        while True:
            yield from self._await_up(machine, run)
            start = self._calendar_start(unit, self._expected_setup(machine, unit))
            if start is None:
                yield self.env.event()  # the calendar ran out: never starts (censored)
                continue
            if unit.calendar is not None:
                run.calendar_time = start
            if start > self.env.now:
                yield from self._wait_until(start)
                continue
            asked = self.env.now
            needs = _hold_at_start(run, self._expected_setup(machine, unit))
            yield from self._acquire(needs, run)
            if self.env.now == asked or self._can_start_now(machine, unit):
                break
            self._release(needs)
        run.start = self.env.now
        run.expected_setup = self._expected_setup(machine, unit)
        self._record_lags(unit.attached)

    def _can_start_now(self, machine: MachineRuntime, unit: Unit) -> bool:
        """After a late operator grant: machine still up and the calendar still allows."""
        if machine.down:
            return False
        return self._calendar_start(unit, self._expected_setup(machine, unit)) == self.env.now

    def _expected_setup(self, machine: MachineRuntime, unit: Unit) -> float:
        """Setup function on the actual predecessor + D-074 warm-up on actual idle."""
        if unit.in_progress:
            return 0.0
        setup = self.shop.setups.seconds(machine.id, machine.last_op, unit.key.id)
        restart = unit.machine.restart
        if restart is not None and self.env.now - machine.last_end > restart.idle_threshold_seconds:
            setup += restart.warmup_seconds
        return setup

    def _calendar_start(self, unit: Unit, setup: float) -> float | None:
        """Earliest start from now by the unit's calendar rule (REQ-129, D-089)."""
        if unit.rule == "forbid":
            return _earliest_fit(unit.calendar, self.env.now, setup + unit.run_planned)
        return next_working(unit.calendar, self.env.now)

    def _draw(self, run: _UnitRun) -> None:
        """Factors when the unit starts, keyed by its lowest member id (D-095)."""
        unit, key = run.unit, run.unit.key
        ctx = OpContext(
            work_center_id=unit.machine.work_center_id,
            machine_id=unit.machine.id,
            family=key.setup_family or key.batch_family,
            quantity=key.quantity,
        )
        run.run_factor = self.draws.run_factor(ctx, key.id)
        run.setup_seconds = run.expected_setup * self.draws.setup_factor(ctx, key.id)
        run.run_seconds = unit.run_planned * run.run_factor

    # --- waiting ---------------------------------------------------------------------------------

    def _wait_until(self, time: float) -> Steps:
        if time > self.env.now:
            yield self.env.timeout(time - self.env.now)

    def _await_gates(self, ops: Sequence[ShopOperation]) -> Steps:
        for op in ops:
            gate = self.gates[op.id]
            if gate.pending:
                gate.event = gate.event or self.env.event()
                yield gate.event
            yield from self._wait_until(gate.time)

    def _await_up(self, machine: MachineRuntime, run: _UnitRun) -> Steps:
        while machine.down and machine.came_up is not None:
            yield machine.came_up
            run.machine_up_time = self.env.now

    def _acquire(self, needs: PoolNeeds, run: _UnitRun) -> Steps:
        """Take operators pool by pool in pool-id order, by the unit's priority (D-088)."""
        for pool_id, quantity in needs:
            yield self.pools[pool_id].acquire(quantity, run.unit.priority)
        if needs:
            run.operator_time = self.env.now

    def _swap_operators(self, run: _UnitRun) -> Steps:
        """At the setup end: setup-phase operators go, run-phase operators come (a late
        grant holds the machine; the wait shows in `operator_time`)."""
        unit = run.unit
        if run.setup_held:
            self._release(unit.setup_needs)
        if run.run_seconds > 0.0:
            run.run_held = True
            yield from self._acquire(unit.run_needs, run)

    def _release(self, needs: PoolNeeds) -> None:
        for pool_id, quantity in needs:
            self.pools[pool_id].release(quantity)

    def _record_lags(self, ops: Sequence[ShopOperation]) -> None:
        """Max-lag excess at the material gate instant g = now (D-090: recorded only)."""
        for op in ops:
            limits = self.gates[op.id].limits
            if limits:
                self.lag_violation[op.id] = max(0.0, self.env.now - min(limits))

    # --- work ------------------------------------------------------------------------------------

    def _work(
        self,
        machine: MachineRuntime,
        run: _UnitRun,
        seconds: float,
        milestones: Sequence[_Milestone],
    ) -> Steps:
        """Advance `seconds` of drawn work, preempt-resume on outages (D-094)."""
        calendar = run.unit.calendar if run.unit.rule == "pause" else None
        done, fired = 0.0, 0
        while True:
            fired = self._fire(milestones, fired, done)
            if done >= seconds:
                return
            yield from self._await_resume(machine, run)
            target = seconds if fired == len(milestones) else milestones[fired].threshold
            finish = finish_after_work(calendar, self.env.now, target - done)
            if finish is None:
                yield self.env.event()  # the calendar ran out mid-work (censored)
                continue
            began = self.env.now
            yield from self._segment(machine, finish - began)
            if machine.down:
                done = min(target, done + work_seconds(calendar, began, self.env.now))
            else:
                done = target

    def _segment(self, machine: MachineRuntime, seconds: float) -> Steps:
        """One stretch of work; ends early when the machine goes down."""
        if machine.went_down is None:
            yield self.env.timeout(seconds)
        else:
            yield self.env.timeout(seconds) | machine.went_down

    def _await_resume(self, machine: MachineRuntime, run: _UnitRun) -> Steps:
        while machine.down and machine.came_up is not None:
            began = self.env.now
            yield machine.came_up
            run.interrupted += self.env.now - began

    def _fire(self, milestones: Sequence[_Milestone], fired: int, done: float) -> int:
        """Open the overlap links whose transfer lot is done (D-091); returns the count."""
        now = self.env.now
        while fired < len(milestones) and milestones[fired].threshold <= done:
            link = milestones[fired].link
            limit = None if link.max_lag is None else now + link.max_lag
            self._satisfy(milestones[fired].succ_id, now + link.min_lag, limit)
            fired += 1
        return fired

    def _milestones(self, run: _UnitRun) -> list[_Milestone]:
        """Transfer instants in drawn run work: factor x (fixed + q x unit), capped."""
        found: list[_Milestone] = []
        for op, option in zip(run.unit.members, run.unit.options, strict=True):
            for succ_id, link in self.successors[op.id]:
                if link.overlap_quantity is None:
                    continue
                planned = option.fixed_seconds + link.overlap_quantity * option.unit_seconds
                threshold = min(run.run_seconds, run.run_factor * planned)
                found.append(_Milestone(threshold, succ_id, link))
        found.sort(key=lambda m: m.threshold)
        return found

    def _last_lot(self, run: _UnitRun) -> Steps:
        """End no earlier than pred.end + q x own unit x own factor per overlap link."""
        for op, option in zip(run.unit.members, run.unit.options, strict=True):
            for link in op.predecessors:
                if link.overlap_quantity is None or link.operation_id not in self.successors:
                    continue
                if link.operation_id not in self.ends:
                    yield self._finished_event(link.operation_id)
                lot = link.overlap_quantity * option.unit_seconds * run.run_factor
                yield from self._wait_until(self.ends[link.operation_id] + lot)

    def _finished_event(self, op_id: str) -> simpy.Event:
        event = self._finished.get(op_id)
        if event is None:
            event = self._finished[op_id] = self.env.event()
        return event

    def _finish(self, machine: MachineRuntime, run: _UnitRun) -> None:
        end = self.env.now
        machine.last_end = end
        machine.last_op = run.unit.members[-1].id
        for op in run.unit.members:
            self.ends[op.id] = end
            for succ_id, link in self.successors[op.id]:
                if link.overlap_quantity is None:
                    limit = None if link.max_lag is None else end + link.max_lag
                    self._satisfy(succ_id, end + link.min_lag, limit)
            waiting = self._finished.pop(op.id, None)
            if waiting is not None:
                waiting.succeed()
        self.remaining -= len(run.unit.members)
        self.sequencer.unit_done(run.unit)

    def _satisfy(self, op_id: str, ready_at: float, limit: float | None) -> None:
        """Meet one link of `op_id`'s gate; tell the sequencer when the gate opens."""
        gate = self.gates[op_id]
        if gate.satisfy(ready_at, limit):
            self.sequencer.gate_open(op_id, gate.time)

    # --- records ---------------------------------------------------------------------------------

    def records(self, end_time: float) -> tuple[OpRecord, ...]:
        """One record per pending operation, in the shop's document order."""
        unstarted = {op.id: unit for unit in self.sequencer.unstarted() for op in unit.members}
        found: list[OpRecord] = []
        for op in self.shop.operations.values():
            run = self.runs.get(op.id)
            if run is None:
                found.append(self._unstarted_record(op, unstarted[op.id], end_time))
            else:
                found.append(self._record(op, run, end_time))
        return tuple(found)

    def _record(self, op: ShopOperation, run: _UnitRun, end_time: float) -> OpRecord:
        unit = run.unit
        end = self.ends.get(op.id)
        censored = end is None
        start = end_time if run.start is None else run.start
        stop = end_time if end is None else end
        overrun = 0.0
        if not censored and unit.rule == "forbid":
            overrun = max(0.0, stop - _working_run_end(unit.calendar, start))
        return OpRecord(
            operation_id=op.id,
            job_id=op.job_id,
            machine_id=unit.machine.id,
            batch_id=unit.batch_id,
            planned_start=unit.planned_start,
            planned_end=unit.planned_end,
            gate_time=self._gate_time(op),
            machine_free_time=run.machine_free,
            operator_time=run.operator_time,
            calendar_time=run.calendar_time,
            machine_up_time=run.machine_up_time,
            start=start,
            setup_end=end_time if run.setup_end is None else run.setup_end,
            end=stop,
            setup_seconds=run.setup_seconds,
            run_seconds=run.run_seconds,
            interrupted_seconds=run.interrupted,
            overrun_seconds=overrun,
            max_lag_violation_seconds=self.lag_violation.get(op.id, 0.0),
            pin_window_missed=_missed_pin(op, start),
            censored=censored,
        )

    def _unstarted_record(self, op: ShopOperation, unit: Unit, end_time: float) -> OpRecord:
        return OpRecord(
            operation_id=op.id,
            job_id=op.job_id,
            machine_id=unit.machine.id,
            batch_id=unit.batch_id,
            planned_start=unit.planned_start,
            planned_end=unit.planned_end,
            gate_time=self._gate_time(op),
            machine_free_time=None,
            operator_time=None,
            calendar_time=None,
            machine_up_time=None,
            start=end_time,
            setup_end=end_time,
            end=end_time,
            setup_seconds=0.0,
            run_seconds=0.0,
            interrupted_seconds=0.0,
            overrun_seconds=0.0,
            max_lag_violation_seconds=0.0,
            pin_window_missed=_missed_pin(op, end_time),
            censored=True,
        )

    def _gate_time(self, op: ShopOperation) -> float | None:
        gate = self.gates[op.id]
        return None if gate.pending else gate.time


def _hold_at_start(run: _UnitRun, setup: float) -> PoolNeeds:
    """Operators to take at the start; a zero-length phase takes nobody, as the verifier's
    pool sweep skips empty `_need_span`s."""
    unit = run.unit
    run.setup_held = setup > 0.0
    run.both_held = run.setup_held or unit.run_planned > 0.0
    if run.setup_held:
        return unit.start_needs
    return unit.both_needs if run.both_held else ()


def _missed_pin(op: ShopOperation, start: float) -> bool:
    """True when the op started (or was still waiting) after its pin's latest start."""
    latest = None if op.pin is None else op.pin.latest_start
    return latest is not None and start > latest + TOLERANCE


def _next_interval_start(calendar: Intervals, t: float) -> float | None:
    """Start of the first calendar interval that begins after `t`; None when none."""
    if calendar is None:
        return None
    index = bisect_right(calendar, t, key=lambda interval: interval[0])
    return calendar[index][0] if index < len(calendar) else None


def _earliest_fit(calendar: Intervals, t: float, length: float) -> float | None:
    """Earliest start >= t where [start, start + length) fits one working run (`forbid`).

    The baseline's search (`timeline._fit_between`): next working instant, else the next
    interval start. None when the calendar has no such run.
    """
    candidate: float | None = t
    while candidate is not None:
        start = next_working(calendar, candidate)
        if start is None:
            return None
        if fits_within(calendar, start, start + length):
            return start
        candidate = _next_interval_start(calendar, start)
    return None


def _working_run_end(calendar: Intervals, t: float) -> float:
    """End of the continuous working run holding `t` (touching intervals join); `t` itself
    when it is not working; +inf without a calendar. A `forbid` op past it overran."""
    if calendar is None:
        return float("inf")
    index = bisect_right(calendar, t, key=lambda interval: interval[0]) - 1
    if index < 0 or t >= calendar[index][1]:
        return t
    reach = calendar[index][1]
    while index + 1 < len(calendar) and calendar[index + 1][0] == reach:
        index += 1
        reach = calendar[index][1]
    return reach


__all__ = ["Floor", "RepDraws"]
