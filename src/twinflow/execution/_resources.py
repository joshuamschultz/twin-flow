"""Shared resources of one run: operator pools, machine up/down state, outages (COMP-135).

`OperatorPool` grants all-or-nothing requests strictly by priority (planned start, then
id, D-088), at the end of each instant: a request that does not fit blocks those behind
it, so a low-priority op never takes the operators a waiting higher-priority op needs.
Pools are acquired in pool-id order, which rules out a wait cycle between two ops.

`MachineRuntime` tracks outages as a down counter, so overlapping breakdowns and scripted
outages count once. Breakdowns are clock-time failures by the D-094 recurrence on keyed
draws, independent of any plan (D-095).
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Generator, Iterator
from itertools import count

import simpy
from simpy.events import EventPriority

from twinflow.engine.rng import SOURCE_FAILURE, SOURCE_REPAIR, keyed_uniform
from twinflow.execution.distributions import quantile
from twinflow.execution.policy import BreakdownRule

LATE = EventPriority(2)
"""SimPy event priority after NORMAL (1): a grant pass runs once every other event of the
same instant has been processed, so requests made at one instant compete by priority."""


class _AtEndOfInstant(simpy.Event):
    """An event processed after every NORMAL event of the current instant (SimPy's own
    `Timeout` and `Initialize` schedule themselves the same way, with their priority)."""

    def __init__(self, env: simpy.Environment) -> None:
        super().__init__(env)
        self._ok = True
        self._value = None
        env.schedule(self, LATE)


class OperatorPool:
    """`size` operators granted by priority, all-or-nothing per request.

    Grants happen in a pass at the end of the instant (`LATE`), never inside `acquire` or
    `release`: two ops that ask at the same time are served by priority, not by which
    machine process happened to run first. A grant at the same instant costs no time.
    """

    __slots__ = ("_env", "_order", "_pass_pending", "_waiting", "free")

    def __init__(self, env: simpy.Environment, size: int) -> None:
        self._env = env
        self.free = size
        self._waiting: list[tuple[tuple[float, str], int, int, simpy.Event]] = []
        self._order = count()
        self._pass_pending = False

    def acquire(self, quantity: int, priority: tuple[float, str]) -> simpy.Event:
        """An event that succeeds when the operators are granted."""
        granted = self._env.event()
        heapq.heappush(self._waiting, (priority, next(self._order), quantity, granted))
        self._request_pass()
        return granted

    def release(self, quantity: int) -> None:
        """Return operators; waiting requests are granted at the end of the instant."""
        self.free += quantity
        if self._waiting:
            self._request_pass()

    def _request_pass(self) -> None:
        if not self._pass_pending:
            self._pass_pending = True
            _AtEndOfInstant(self._env).callbacks.append(self._grant)

    def _grant(self, _event: simpy.Event) -> None:
        self._pass_pending = False
        while self._waiting and self._waiting[0][2] <= self.free:
            _, _, quantity, granted = heapq.heappop(self._waiting)
            self.free -= quantity
            granted.succeed()


class MachineRuntime:
    """A machine's state during one run: last occupation, outages, downtime."""

    __slots__ = (
        "_down_count",
        "_down_since",
        "_env",
        "came_up",
        "downtime",
        "id",
        "last_end",
        "last_op",
        "went_down",
    )

    def __init__(self, env: simpy.Environment, machine_id: str, free_from: float) -> None:
        self._env = env
        self.id = machine_id
        self.last_end = free_from
        self.last_op: str | None = None
        self.downtime = 0.0
        self._down_count = 0
        self._down_since = 0.0
        self.came_up: simpy.Event | None = None
        self.went_down: simpy.Event | None = None

    @property
    def down(self) -> bool:
        """True while any outage holds the machine."""
        return self._down_count > 0

    def may_fail(self) -> None:
        """Declare that outages can hit this machine: work then watches `went_down`."""
        if self.went_down is None:
            self.went_down = self._env.event()

    def go_down(self) -> None:
        """An outage starts; the first one stops the machine and wakes its work."""
        self._down_count += 1
        if self._down_count > 1:
            return
        self._down_since = self._env.now
        self.came_up = self._env.event()
        signal, self.went_down = self.went_down, self._env.event()
        if signal is not None:
            signal.succeed()

    def go_up(self) -> None:
        """An outage ends; the last one restarts the machine."""
        self._down_count -= 1
        if self._down_count > 0:
            return
        self.downtime += self._env.now - self._down_since
        signal, self.came_up = self.came_up, None
        if signal is not None:
            signal.succeed()

    def downtime_until(self, end_time: float) -> float:
        """Downtime within [now, end_time]: an outage still running is cut at `end_time`."""
        if self.down:
            return self.downtime + max(0.0, end_time - self._down_since)
        return self.downtime


def failure_iter(
    machine_id: str, rule: BreakdownRule, seed: int, replication: int, now: float
) -> Iterator[tuple[float, float]]:
    """Endless (start, duration) failures of a machine by the D-094 recurrence.

    Failure k (0-based) starts `mtbf * -ln(1 - u)` after the previous repair ended (after
    `now` for k = 0) and lasts `quantile(rule.repair, u')`, with u and u' keyed draws on
    `f"{machine}:{k}"`. Only the seed and replication are read: failures pair across plans.
    """
    earliest = now
    for k in count():
        key = f"{machine_id}:{k}"
        gap = -rule.mtbf_seconds * math.log(
            1.0 - keyed_uniform(seed, replication, SOURCE_FAILURE, key)
        )
        start = earliest + gap
        duration = quantile(rule.repair, keyed_uniform(seed, replication, SOURCE_REPAIR, key))
        yield start, duration
        earliest = start + duration


def outage_process(
    env: simpy.Environment, machine: MachineRuntime, start: float, duration: float
) -> Generator[simpy.Event, object, None]:
    """One outage: down at `start` (or now when it began earlier) until `start + duration`."""
    end = start + duration
    if end <= env.now:
        return
    if start > env.now:
        yield env.timeout(start - env.now)
    machine.go_down()
    yield env.timeout(end - env.now)
    machine.go_up()


def breakdown_process(
    env: simpy.Environment, machine: MachineRuntime, failures: Iterator[tuple[float, float]]
) -> Generator[simpy.Event, object, None]:
    """Fail and repair the machine forever; the run loop stops when the work is done."""
    for start, duration in failures:
        yield env.timeout(max(0.0, start - env.now))
        machine.go_down()
        yield env.timeout(duration)
        machine.go_up()


__all__ = [
    "MachineRuntime",
    "OperatorPool",
    "breakdown_process",
    "failure_iter",
    "outage_process",
]
