"""SimPy executor: a verified plan or a dispatch rule run on the shop (COMP-135).

`execute(shop, control, draws, outages, limits)` runs one replication and returns an
`ExecutionRun` of one `OpRecord` per pending operation. The control decides only which
unit each machine takes next (`_units.Sequencer`); everything after that decision is the
shared floor code in `_floor.py` (D-088, D-102). `PlanControl` runs each machine through
its planned assignments in the verifier's order `(start, end, operation id)`, a batch as
one unit, with the planned start as an earliest start: late work right-shifts its
successors and nothing is resequenced (D-088). `DispatchControl` (`_dispatch.py`) picks
each idle machine's next unit by a dispatch rule instead (D-102).

Randomness comes only from keyed draws (D-095): run and setup factors keyed by operation
id, failures and repairs by `machine:k` (D-094), and a source's scripted outage `i` fires
when `keyed_uniform(seed, rep, SOURCE_OUTAGE, str(i)) <= probability`. Outages passed to
`execute` always apply (the robustness probes). The run stops when every operation is
done, at the event cap or after `stop_time` (T_c, D-107, D-109); unfinished operations
are censored. No file or network I/O.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import takewhile
from typing import Literal

import simpy

from twinflow.engine.rng import SOURCE_OUTAGE, keyed_uniform
from twinflow.execution._dispatch import DispatchControl
from twinflow.execution._floor import Floor, RepDraws
from twinflow.execution._resources import (
    MachineRuntime,
    breakdown_process,
    failure_iter,
    outage_process,
)
from twinflow.execution._units import Control, Sequencer, Unit, build_unit
from twinflow.execution.metrics import OpRecord
from twinflow.execution.policy import BreakdownRule, ScriptedOutage
from twinflow.execution.shop import ShopModel, censor_time
from twinflow.scheduling.model import Assignment, Schedule
from twinflow.scheduling.verifier import TOLERANCE

Termination = Literal["completed", "event_limit", "calendar_end"]

EVENTS_PER_OPERATION = 50
EVENTS_BASE = 10_000


@dataclass(frozen=True)
class Limits:
    """Bounds of one run (D-109): at most `max_events` SimPy events, nothing after
    `stop_time` (T_c, D-107)."""

    max_events: int
    stop_time: float

    @classmethod
    def for_shop(cls, shop: ShopModel, planned_makespan: float = 0.0) -> Limits:
        """Event cap 50 x operations + 10,000; stop at `censor_time(shop, makespan)`.

        `planned_makespan` (planned last end minus now) only matters for a shop that some
        operation can run on without a calendar; 0 gives the 30-day floor.
        """
        max_events = EVENTS_PER_OPERATION * len(shop.operations) + EVENTS_BASE
        return cls(max_events=max_events, stop_time=censor_time(shop, planned_makespan))


@dataclass(frozen=True)
class ExecutionRun:
    """The outcome of one replication.

    `downtime_by_machine` holds every machine's down seconds within [now, end_time];
    `horizon_violations` names, in record order, the finished operations that end after
    `horizon_end` (D-090: recorded, not enforced).
    """

    records: tuple[OpRecord, ...]
    downtime_by_machine: Mapping[str, float]
    termination: Termination
    events: int
    end_time: float
    horizon_violations: tuple[str, ...]


def failure_times(
    machine_id: str, rule: BreakdownRule, draws: RepDraws, now: float, until: float
) -> tuple[tuple[float, float], ...]:
    """The machine's (start, duration) failures starting before `until` (D-094).

    Reads only `draws.seed` and `draws.replication`: failures pair across plans.
    """
    failures = failure_iter(machine_id, rule, draws.seed, draws.replication, now)
    return tuple(takewhile(lambda failure: failure[0] < until, failures))


class PlanControl:
    """Plan control (D-088): planned machine, planned order, planned start as earliest.

    The schedule must assign every pending operation of the shop exactly once to one of
    its option machines; anything else raises `ValueError` when a run starts (callers
    verify plans first, so this guards against a plan of another problem).
    """

    def __init__(self, schedule: Schedule) -> None:
        self.schedule = schedule

    def sequencer(self, shop: ShopModel, env: simpy.Environment) -> Sequencer:
        """A fresh per-run queue of each machine's planned units."""
        _check_coverage(shop, self.schedule.assignments)
        return _PlanSequencer(_plan_units(shop, self.schedule.assignments))


class _PlanSequencer:
    """Hands each machine its planned units in order."""

    def __init__(self, units: Mapping[str, Sequence[Unit]]) -> None:
        self._queues = {machine_id: deque(queue) for machine_id, queue in units.items()}

    def next_unit(self, machine: MachineRuntime) -> Unit | simpy.Event | None:
        """The next planned unit, or None after the last one."""
        queue = self._queues[machine.id]
        return queue.popleft() if queue else None

    def gate_open(self, operation_id: str, time: float) -> None:
        """Plan control waits on gates in the floor; nothing to track."""

    def unit_done(self, unit: Unit) -> None:
        """Plan control follows the plan; nothing to track."""

    def machine_ids(self) -> tuple[str, ...]:
        """Machines with planned work."""
        return tuple(sorted(self._queues))

    def unstarted(self) -> tuple[Unit, ...]:
        """Planned units no machine reached."""
        return tuple(unit for queue in self._queues.values() for unit in queue)


def _check_coverage(shop: ShopModel, assignments: Iterable[Assignment]) -> None:
    """Every pending op exactly once, on one of its options; no other op."""
    seen: set[str] = set()
    for a in assignments:
        op = shop.operations.get(a.operation_id)
        if op is None:
            raise ValueError(f"{a.operation_id}: not a pending operation of the shop")
        if a.operation_id in seen:
            raise ValueError(f"{a.operation_id}: assigned more than once")
        if all(option.machine_id != a.machine_id for option in op.options):
            raise ValueError(f"{a.operation_id}: machine {a.machine_id} is not an option")
        seen.add(a.operation_id)
    missing = sorted(set(shop.operations) - seen)
    if missing:
        raise ValueError(f"pending operations without an assignment: {', '.join(missing[:10])}")


def _plan_units(shop: ShopModel, assignments: Iterable[Assignment]) -> dict[str, list[Unit]]:
    """Each machine's units in the verifier's order; one batch id is one unit."""
    by_machine: dict[str, list[Assignment]] = {}
    for a in assignments:
        by_machine.setdefault(a.machine_id, []).append(a)
    units: dict[str, list[Unit]] = {}
    for machine_id, sequence in by_machine.items():
        sequence.sort(key=lambda a: (a.start, a.end, a.operation_id))
        units[machine_id] = [_unit(shop, machine_id, group) for group in _groups(sequence)]
    return units


def _groups(sequence: Sequence[Assignment]) -> list[list[Assignment]]:
    """Assignments grouped into units: members of one batch id join the first one's unit."""
    groups: list[list[Assignment]] = []
    batches: dict[str, list[Assignment]] = {}
    for a in sequence:
        if a.batch_id is not None and a.batch_id in batches:
            batches[a.batch_id].append(a)
            continue
        group = [a]
        groups.append(group)
        if a.batch_id is not None:
            batches[a.batch_id] = group
    return groups


def _unit(shop: ShopModel, machine_id: str, group: Sequence[Assignment]) -> Unit:
    first = group[0]
    return build_unit(
        shop,
        machine_id,
        [shop.operations[a.operation_id] for a in group],
        batch_id=first.batch_id,
        planned_start=first.start,
        planned_end=first.end,
        priority_time=first.start,
    )


def execute(
    shop: ShopModel,
    control: Control,
    draws: RepDraws,
    outages: Sequence[ScriptedOutage],
    limits: Limits,
) -> ExecutionRun:
    """Run one replication of `shop` under `control` (COMP-135)."""
    env = simpy.Environment(initial_time=shop.now)
    floor = Floor(env, shop, draws, control.sequencer(shop, env))
    _start_outages(env, floor, draws, outages)
    floor.start()
    termination, events = _run(env, floor, limits)
    end_time = limits.stop_time if termination == "calendar_end" else env.now
    records = floor.records(end_time)
    return ExecutionRun(
        records=records,
        downtime_by_machine={m.id: m.downtime_until(end_time) for m in floor.machines.values()},
        termination=termination,
        events=events,
        end_time=end_time,
        horizon_violations=_horizon_violations(shop.horizon_end, records),
    )


def _run(env: simpy.Environment, floor: Floor, limits: Limits) -> tuple[Termination, int]:
    """Step until every operation is done, the event cap, or nothing left before T_c."""
    events = 0
    while floor.remaining:
        if env.peek() > limits.stop_time:
            return "calendar_end", events
        if events >= limits.max_events:
            return "event_limit", events
        env.step()
        events += 1
    return "completed", events


def _start_outages(
    env: simpy.Environment, floor: Floor, draws: RepDraws, outages: Sequence[ScriptedOutage]
) -> None:
    """Explicit outages, the source's scripted outages that fire, and breakdowns."""
    for outage in outages:
        if outage.machine_id not in floor.machines:
            raise ValueError(f"outage on unknown machine {outage.machine_id}")
        _start_outage(env, floor.machines[outage.machine_id], outage)
    for index, outage in enumerate(draws.source.outages()):
        u = keyed_uniform(draws.seed, draws.replication, SOURCE_OUTAGE, str(index))
        # A profile outage on a machine this problem lacks has nothing to stop.
        if u <= outage.probability and outage.machine_id in floor.machines:
            _start_outage(env, floor.machines[outage.machine_id], outage)
    for machine in floor.shop.machines.values():
        rule = draws.source.breakdown(machine.id, machine.work_center_id)
        if rule is not None:
            runtime = floor.machines[machine.id]
            runtime.may_fail()
            failures = failure_iter(machine.id, rule, draws.seed, draws.replication, env.now)
            env.process(breakdown_process(env, runtime, failures))


def _start_outage(env: simpy.Environment, machine: MachineRuntime, outage: ScriptedOutage) -> None:
    machine.may_fail()
    env.process(outage_process(env, machine, outage.start, outage.duration))


def _horizon_violations(horizon_end: float | None, records: Iterable[OpRecord]) -> tuple[str, ...]:
    if horizon_end is None:
        return ()
    return tuple(
        r.operation_id for r in records if not r.censored and r.end > horizon_end + TOLERANCE
    )


__all__ = [
    "Control",
    "DispatchControl",
    "ExecutionRun",
    "Limits",
    "OpRecord",
    "PlanControl",
    "RepDraws",
    "Sequencer",
    "Termination",
    "execute",
    "failure_times",
]
