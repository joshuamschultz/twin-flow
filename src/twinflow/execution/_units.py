"""Units of work and the sequencing seam shared by plan and dispatch control (COMP-135).

A `Unit` is one start on one machine: a single operation, or the members of one batch
(one setup, one run draw, D-088). Everything the executor needs about it is resolved once
by `build_unit`, so plan control and dispatch control hand the same object
to the same machine code. The only difference between the two controls is *which* unit a
machine takes next: that decision is the `Sequencer` protocol.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

import simpy

from twinflow.execution._resources import MachineRuntime
from twinflow.execution.shop import ShopMachine, ShopModel, ShopOperation
from twinflow.scheduling import calendar_math
from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import ProcessingOption

Rule = Literal["forbid", "pause", "finish_unattended"]
PoolNeeds = tuple[tuple[str, int], ...]
"""(pool id, operators) per pool, sorted by pool id (the deadlock-free acquire order)."""


@dataclass(frozen=True, slots=True)
class Unit:
    """One start on one machine, with every value the executor reads (D-088).

    `members` are in the verifier's machine order; the last one is the setup predecessor
    of the machine's next unit (`verifier._setup_predecessors`). `key` is the member with
    the lowest id: it keys the draws (D-095), gives the factor context, the setup and the
    planned run. Operators follow the verifier's `_need_span`: `start_needs` (setup and
    both phases, merged in pool-id order) are taken at the start, `setup_needs` given back
    at the setup end where `run_needs` are taken, the rest given back at the end; a phase
    of zero length takes nobody (the verifier skips empty spans). An in-progress unit
    runs `remaining_seconds` from now with no setup, no gate and no calendar (the
    verifier's `in_progress` rule).
    """

    machine: ShopMachine
    members: tuple[ShopOperation, ...]
    key: ShopOperation
    batch_id: str | None
    planned_start: float | None
    planned_end: float | None
    priority: tuple[float, str]
    in_progress: bool
    rule: Rule
    calendar: Intervals
    run_planned: float
    options: tuple[ProcessingOption, ...]
    start_needs: PoolNeeds
    setup_needs: PoolNeeds
    run_needs: PoolNeeds
    both_needs: PoolNeeds
    attached: tuple[ShopOperation, ...]
    detached: tuple[ShopOperation, ...]


def option_on(op: ShopOperation, machine_id: str) -> ProcessingOption:
    """The op's processing option on `machine_id` (ValueError when it is not an option)."""
    for option in op.options:
        if option.machine_id == machine_id:
            return option
    raise ValueError(f"{op.id}: machine {machine_id} is not an option")


def build_unit(
    shop: ShopModel,
    machine_id: str,
    members: Sequence[ShopOperation],
    *,
    batch_id: str | None,
    planned_start: float | None,
    planned_end: float | None,
    priority_time: float,
) -> Unit:
    """Resolve one unit; `priority_time` orders its operator requests (D-088)."""
    machine = shop.machines[machine_id]
    key = min(members, key=lambda op: op.id)
    options = tuple(option_on(op, machine_id) for op in members)
    in_progress = key.status.state == "in_progress"
    key_option = options[members.index(key)]
    run_planned = (
        key.status.remaining_seconds or 0.0
        if in_progress
        else key_option.fixed_seconds + key.quantity * key_option.unit_seconds
    )
    return Unit(
        machine=machine,
        members=tuple(members),
        key=key,
        batch_id=batch_id,
        planned_start=planned_start,
        planned_end=planned_end,
        priority=(priority_time, key.id),
        in_progress=in_progress,
        rule="finish_unattended" if in_progress else _rule(key.shift_crossing),
        calendar=None if in_progress else _calendar(shop, machine_id, members),
        run_planned=run_planned,
        options=options,
        start_needs=_needs(members, ("setup", "both")),
        setup_needs=_needs(members, ("setup",)),
        run_needs=_needs(members, ("run",)),
        both_needs=_needs(members, ("both",)),
        attached=() if in_progress else tuple(op for op in members if not op.detached),
        detached=() if in_progress else tuple(op for op in members if op.detached),
    )


def _rule(shift_crossing: str) -> Rule:
    if shift_crossing == "pause":
        return "pause"
    if shift_crossing == "finish_unattended":
        return "finish_unattended"
    return "forbid"


def _calendar(shop: ShopModel, machine_id: str, members: Sequence[ShopOperation]) -> Intervals:
    """Effective calendar of the unit: each member's (machine ∩ its pools), intersected.

    A single operation (the common case) gets its memoized calendar unchanged.
    """
    result = shop.effective_calendar(machine_id, members[0].id)
    for op in members[1:]:
        result = calendar_math.intersect(result, shop.effective_calendar(machine_id, op.id))
    return result


def _needs(members: Iterable[ShopOperation], phases: tuple[str, ...]) -> PoolNeeds:
    """Operators per pool for the given phases, summed over members (each member counts,
    as the verifier's pool sweep counts each assignment)."""
    total: dict[str, int] = {}
    for op in members:
        for need in op.operator_needs:
            if need.phase in phases:
                total[need.pool_id] = total.get(need.pool_id, 0) + need.quantity
    return tuple(sorted(total.items()))


class Sequencer(Protocol):
    """Decides which unit each machine takes next; everything after that is shared.

    The floor asks with the idle machine's runtime (up/down state, last operation) and
    reports the two changes a sequencer cannot see itself: an operation's material gate
    opening and a unit finishing (D-102).
    """

    def next_unit(self, machine: MachineRuntime) -> Unit | simpy.Event | None:
        """The machine's next unit, an event to wait on before asking again, or None
        when the machine has nothing more to do."""
        ...

    def gate_open(self, operation_id: str, time: float) -> None:
        """Every predecessor link of the operation is met: material is there at `time`."""
        ...

    def unit_done(self, unit: Unit) -> None:
        """The unit finished at the current time."""
        ...

    def machine_ids(self) -> tuple[str, ...]:
        """Machines that get a process, in machine-id order."""
        ...

    def unstarted(self) -> tuple[Unit, ...]:
        """Units never handed out (their operations are censored at the run's end)."""
        ...


class Control(Protocol):
    """A sequencing mode of the executor: plan control or dispatch control."""

    def sequencer(self, shop: ShopModel, env: simpy.Environment) -> Sequencer:
        """A fresh sequencer for one run of `shop` (raises ValueError on bad input)."""
        ...


__all__ = ["Control", "PoolNeeds", "Rule", "Sequencer", "Unit", "build_unit", "option_on"]
