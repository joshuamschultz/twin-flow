"""Dispatch control: each idle machine picks its next unit by a dispatch rule (COMP-135).

The dispatch baseline (D-102) runs in the same executor as plan control; only the pick
differs. When machine `m` is idle and up it takes the best ready, unclaimed operation it
is eligible for (an option, restricted to the pin or lock machine; an in-progress
operation only on its status machine, handed out first) by the rule key, ties by
operation id. Rule keys, smaller first:

- `fifo`: material gate time;
- `edd`: job due (none = +inf);
- `spt`: setup on `m` from its actual predecessor (`SetupTable`, no warm-up) + planned
  run on `m`;
- `critical_ratio`: (due - now) / expected remaining work of the job, where the work is
  the sum over its unfinished operations (the candidate included) of the minimum planned
  run over their options, setups excluded; no due = +inf.

"Ready" means the material gate is open (the floor reports it) and a pin's earliest start
has passed; `hold until` is already the job release. Each machine has an index of its
ready eligible operations, so a pick scans that machine's queue only. When an operation
becomes ready, its idle eligible machines are woken in machine-id order. A picked
batch-family operation on a batch machine gathers ready same-family operations of that
index in rule order, first fit: one that would exceed the capacity is skipped and a
smaller later one can still join. Operators are requested by (pick time, key id). No
file or network I/O; every container is insertion-ordered, so runs are reproducible.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Iterable, Sequence

import simpy

from twinflow.execution._resources import MachineRuntime
from twinflow.execution._units import Sequencer, Unit, build_unit, option_on
from twinflow.execution.shop import ShopModel, ShopOperation
from twinflow.policy import DISPATCH_NAMES
from twinflow.scheduling.verifier import TOLERANCE

Key = tuple[float, str]
"""Rule value, then operation id: the total order of a pick."""

RULES = ("fifo", "edd", "spt", "critical_ratio")
"""The rules this module has a key for; a shared rule name without one is refused."""


class DispatchControl:
    """Dispatch control by one rule of `twinflow.policy.DISPATCH_NAMES` (D-102).

    Raises `ValueError` for any other rule name. One control runs any number of
    replications: each `sequencer` call starts a fresh run.
    """

    def __init__(self, rule: str) -> None:
        if rule not in DISPATCH_NAMES or rule not in RULES:
            raise ValueError(f"unknown dispatch rule {rule!r}; expected one of {DISPATCH_NAMES}")
        self.rule = rule

    def sequencer(self, shop: ShopModel, env: simpy.Environment) -> Sequencer:
        """A fresh per-run dispatcher over every machine of the shop."""
        return _DispatchSequencer(shop, env, self.rule)


class _DispatchSequencer:
    """Per-run dispatch state: ready index per machine, idle machines, job work left."""

    def __init__(self, shop: ShopModel, env: simpy.Environment, rule: str) -> None:
        self._shop = shop
        self._env = env
        self._rule = rule
        ops = shop.operations.values()
        self._eligible = {op.id: _eligible_machines(op) for op in ops}
        self._ready: dict[str, dict[str, None]] = {m: {} for m in sorted(shop.machines)}
        self._idle: dict[str, simpy.Event] = {}
        self._gate_time: dict[str, float] = {}
        self._claimed: set[str] = set()
        self._handed: set[str] = set()
        self._first: dict[str, deque[ShopOperation]] = {m: deque() for m in self._ready}
        self._job_left: dict[str, dict[str, float]] = {}
        for op in ops:
            self._job_left.setdefault(op.job_id, {})[op.id] = _expected_run(op)
            if op.status.state == "in_progress":
                self._first[self._eligible[op.id][0]].append(op)
                self._claimed.add(op.id)
        self._job_work: dict[str, float] = {
            job_id: sum(left.values()) for job_id, left in self._job_left.items()
        }

    # --- the Sequencer protocol ------------------------------------------------------------------

    def next_unit(self, machine: MachineRuntime) -> Unit | simpy.Event | None:
        """In-progress work first; else wait while down or not yet available; else pick."""
        first = self._first[machine.id]
        if first:
            return self._hand(machine.id, [first.popleft()])
        if len(self._claimed) == len(self._shop.operations):
            return None
        if machine.came_up is not None:  # down: pick once it is up again
            return machine.came_up
        if machine.last_end > self._env.now:  # `available_from` still ahead
            return self._env.timeout(machine.last_end - self._env.now)
        unit = self._pick(machine)
        if unit is not None:
            return unit
        idle = self._idle[machine.id] = self._env.event()
        return idle

    def machine_ids(self) -> tuple[str, ...]:
        """Every machine of the shop, in machine-id order."""
        return tuple(self._ready)

    def unstarted(self) -> tuple[Unit, ...]:
        """One unit per operation never handed out, on its first eligible machine (its
        first option when a pin names no option machine: it can never be picked)."""
        return tuple(
            self._unit(
                (*self._eligible[op_id], op.options[0].machine_id)[0],
                [op],
                priority_time=self._shop.now,
            )
            for op_id, op in self._shop.operations.items()
            if op_id not in self._handed
        )

    def gate_open(self, operation_id: str, time: float) -> None:
        """Ready at the gate time or the pin's earliest start, whichever is later."""
        if operation_id in self._claimed:
            return
        self._gate_time[operation_id] = time
        pin = self._shop.operations[operation_id].pin
        earliest = None if pin is None else pin.earliest_start
        ready_at = time if earliest is None else max(time, earliest)
        if ready_at <= self._env.now:
            self._make_ready(operation_id)
            return
        timer = self._env.timeout(ready_at - self._env.now)
        timer.callbacks.append(lambda _event: self._make_ready(operation_id))

    def unit_done(self, unit: Unit) -> None:
        """The members' jobs have less work left (critical ratio)."""
        for op in unit.members:
            left = self._job_left[op.job_id]
            del left[op.id]
            self._job_work[op.job_id] = sum(left.values())

    # --- ready index -----------------------------------------------------------------------------

    def _make_ready(self, op_id: str) -> None:
        """Index the op under each eligible machine; wake idle ones in machine-id order."""
        for machine_id in self._eligible[op_id]:
            self._ready[machine_id][op_id] = None
        for machine_id in self._eligible[op_id]:
            waiting = self._idle.pop(machine_id, None)
            if waiting is not None:
                waiting.succeed()

    def _claim(self, op_ids: Iterable[str]) -> None:
        for op_id in op_ids:
            self._claimed.add(op_id)
            for machine_id in self._eligible[op_id]:
                del self._ready[machine_id][op_id]

    # --- the pick --------------------------------------------------------------------------------

    def _pick(self, machine: MachineRuntime) -> Unit | None:
        """The best ready op on the machine (with its batch), claimed; None when none."""
        ready = self._ready[machine.id]
        if not ready:
            return None

        def key(op_id: str) -> Key:
            return (self._value(machine, op_id), op_id)

        members = self._gather(machine.id, min(ready, key=key), key)
        self._claim(members)
        ops = self._shop.operations
        return self._hand(machine.id, [ops[op_id] for op_id in members])

    def _hand(self, machine_id: str, members: Sequence[ShopOperation]) -> Unit:
        """The unit the machine starts now; its members are no longer unstarted."""
        self._handed.update(op.id for op in members)
        return self._unit(machine_id, members)

    def _value(self, machine: MachineRuntime, op_id: str) -> float:
        """The rule's value of the op on the machine at the current time."""
        op = self._shop.operations[op_id]
        if self._rule == "fifo":
            return self._gate_time[op_id]
        if self._rule == "edd":
            return _due(self._shop, op)
        if self._rule == "spt":
            setup = self._shop.setups.seconds(machine.id, machine.last_op, op_id)
            return setup + _planned_run(op, machine.id)
        return self._critical_ratio(op)

    def _critical_ratio(self, op: ShopOperation) -> float:
        """(due - now) / work left of the job; zero work is +inf, or -inf when overdue."""
        due = _due(self._shop, op)
        if math.isinf(due):
            return due
        slack, work = due - self._env.now, self._job_work[op.job_id]
        if work <= 0.0:
            return -math.inf if slack < 0.0 else math.inf
        return slack / work

    def _gather(self, machine_id: str, first: str, key: Callable[[str], Key]) -> list[str]:
        """`first` plus ready same-family ops in rule order, first fit to the capacity."""
        ops = self._shop.operations
        family = ops[first].batch_family
        batch = self._shop.machines[machine_id].batch_capacity
        if family is None or batch is None:
            return [first]
        same = [o for o in self._ready[machine_id] if o != first and ops[o].batch_family == family]
        members, load = [first], ops[first].quantity
        for op_id in sorted(same, key=key):
            if load + ops[op_id].quantity <= batch.capacity + TOLERANCE:
                members.append(op_id)
                load += ops[op_id].quantity
        return members

    def _unit(
        self,
        machine_id: str,
        members: Sequence[ShopOperation],
        priority_time: float | None = None,
    ) -> Unit:
        """A unit picked now (no planned times); a batch is keyed by its lowest member."""
        batch = self._shop.machines[machine_id].batch_capacity
        key_id = min(op.id for op in members)
        batched = batch is not None and members[0].batch_family is not None
        return build_unit(
            self._shop,
            machine_id,
            members,
            batch_id=f"dispatch:{key_id}" if batched else None,
            planned_start=None,
            planned_end=None,
            priority_time=self._env.now if priority_time is None else priority_time,
        )


def _eligible_machines(op: ShopOperation) -> tuple[str, ...]:
    """Machines that may take the op, by id: its status machine when in progress, else
    its options restricted to the pin (or hard lock) machine."""
    if op.status.state == "in_progress" and op.status.machine_id is not None:
        return (op.status.machine_id,)
    pinned = None if op.pin is None else op.pin.machine_id
    return tuple(
        sorted(o.machine_id for o in op.options if pinned is None or o.machine_id == pinned)
    )


def _due(shop: ShopModel, op: ShopOperation) -> float:
    due = shop.jobs[op.job_id].due
    return math.inf if due is None else due


def _planned_run(op: ShopOperation, machine_id: str) -> float:
    option = option_on(op, machine_id)
    return option.fixed_seconds + op.quantity * option.unit_seconds


def _expected_run(op: ShopOperation) -> float:
    """Minimum planned run over the op's options (critical-ratio work, setups excluded)."""
    return min(o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options)


__all__ = ["DispatchControl"]
