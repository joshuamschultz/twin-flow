"""COMP-012 LaborPool — a named, skilled pool of operators gated by shift calendars.

Unload requests outrank fresh load requests so machines get unblocked before new lots.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Protocol

import simpy
from simpy.resources.resource import PriorityRequest

PRIORITY_UNLOAD = 0
PRIORITY_LOAD = 10


class ShiftCalendar(Protocol):
    """Protocol LaborPool consults for availability; not owned by primitives."""

    def is_on_shift(self, t: float) -> bool: ...

    def next_shift_start(self, t: float) -> float: ...


class LaborPool:
    """request(skill, priority) -> operator handle; shift calendar gates availability."""

    def __init__(
        self,
        name: str,
        headcount: int,
        skills: frozenset[str],
        shift_calendar: ShiftCalendar,
        env: simpy.Environment,
    ) -> None:
        self.name = name
        self.headcount = headcount
        self.skills = skills
        self.shift_calendar = shift_calendar
        self.env = env
        self._resource = simpy.PriorityResource(env, capacity=headcount)

    @property
    def available(self) -> int:
        """Operators free right now — 0 whenever off-shift, held or not."""
        if not self.shift_calendar.is_on_shift(self.env.now):
            return 0
        return int(self._resource.capacity) - self._resource.count

    def request(self, skill: str, priority: int) -> Generator[simpy.Event, None, PriorityRequest]:
        """Block until on-shift and a slot is free, then return a request handle.

        Drive with `yield from` (matches `ResourceAcquirer.acquire()`,
        `engine/acquire.py`). Raises ValueError for a skill this pool does not carry
        (fail-closed) before waiting on anything.
        """
        if skill not in self.skills:
            raise ValueError(f"{self.name!r} labor pool has no {skill!r} skill")

        while not self.shift_calendar.is_on_shift(self.env.now):
            next_start = self.shift_calendar.next_shift_start(self.env.now)
            yield self.env.timeout(next_start - self.env.now)

        request = self._resource.request(priority=priority)
        yield request
        return request

    def release(self, handle: PriorityRequest) -> None:
        """Release a handle returned by `request()`."""
        self._resource.release(handle)
