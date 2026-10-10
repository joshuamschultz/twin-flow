"""Machine and operator-pool timelines for constructive solvers (COMP-009, REQ-075).

Plain mutable state the baseline builds once from the compiled problem; no Pydantic model
is created or validated here (SDD risk note). Times are float seconds.

``MachineTimeline`` (one per machine) holds placed occupations sorted by start; a machine
is busy on ``[start, end)`` and runs one operation or one batch at a time (D-062).
``find_slot`` returns the earliest slot that satisfies, per the SDD Data Model:

- ``start >= max(available_from, earliest_start)`` (callers fold ``now``, pins and the
  job release they want on ``start`` into ``earliest_start``);
- the material gate (D-063): attached setup ``start >= gate_ready``; detached setup
  ``setup_end >= gate_ready``, so the setup may run before the material arrives;
- the setup is ``SetupTable.seconds`` from the previous occupation by start order, or the
  machine state when the slot comes first (D-064);
- gap insertion never moves or reshapes a follower: a gap is used only when the
  follower's setup with the new operation as predecessor equals the follower's recorded
  setup. Every placed operation therefore keeps ``setup == SetupTable.seconds(machine,
  predecessor by start order, op)``, which is what the verifier's ``setup`` check reads;
- calendar (D-065) on the operation's effective calendar (see ``effective_calendar``):
  ``forbid`` puts ``[start, end)`` inside one working run; ``pause`` sets
  ``setup_end = finish_after_work(start, setup)`` and
  ``end = finish_after_work(start, setup + run)``.

Batch machines (``batch_capacity`` set): an operation with a ``batch_family`` first tries
to join an open batch of that family; all members share ``start``, ``setup_end`` and
``end``. A join needs room (total quantity <= capacity), the operation's readiness met at
the batch's times, identical times on the operation's own calendar, and the same setups
on both sides of the batch as every member already has. Otherwise a new batch opens with
the id ``f"{machine_id}#b{n}"``, ``n`` counting batches opened on that machine from 1.

``PoolTimeline`` (one per operator pool) tracks the load of a pool of ``size`` operators
as a step function. Operator needs attach to phases (D-063): ``setup`` holds the pool on
``[start, setup_end)``, ``run`` on ``[setup_end, end)``, ``both`` on ``[start, end)``.
Joint search: pass ``lambda slot: operator_retry(slot, needs, pools)`` as the
``find_slot`` filter; the filter returns None to accept a slot, or a later start to retry
from, so machine gaps and pool capacity are searched together in increasing start order.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from twinflow.scheduling.calendar_math import (
    Interval,
    Intervals,
    finish_after_work,
    fits_within,
    intersect,
    next_working,
)
from twinflow.scheduling.setups import SetupTable

ShiftCrossing: TypeAlias = Literal["forbid", "pause"]
Phase: TypeAlias = Literal["setup", "run", "both"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SlotRequest:
    """What one operation needs from a machine, as plain values.

    ``earliest_start`` bounds ``start``; ``gate_ready`` bounds the material gate
    (``start`` when attached, ``setup_end`` when ``detached``). ``calendar`` is the
    effective calendar (None = always working). ``quantity`` and ``batch_family`` matter
    only on batch machines.
    """

    op_id: str
    run_seconds: float
    earliest_start: float
    gate_ready: float
    quantity: float
    calendar: Intervals = None
    detached: bool = False
    shift_crossing: ShiftCrossing = "forbid"
    batch_family: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Slot:
    """A candidate occupation: setup on ``[start, setup_end)``, run to ``end``.

    ``setup_seconds`` is the setup work (it differs from ``setup_end - start`` only when a
    ``pause`` setup crosses a break). ``batch_id`` is set when the slot joins that batch.
    """

    start: float
    setup_end: float
    end: float
    setup_seconds: float
    batch_id: str | None = None


SlotFilter: TypeAlias = Callable[[Slot], float | None]
"""Accepts a slot (None) or returns a start > slot.start to retry from (inf = never)."""


@dataclass(slots=True)
class _Occupation:
    members: list[str]
    start: float
    setup_end: float
    end: float
    setup_seconds: float
    batch_id: str | None
    batch_family: str | None
    quantity: float


def _interval_start(interval: Interval) -> float:
    return interval[0]


def _next_interval_start(calendar: Intervals, t: float) -> float | None:
    """Start of the first calendar interval that begins after ``t``; None when none."""
    if calendar is None:
        return None
    index = bisect_right(calendar, t, key=_interval_start)
    return calendar[index][0] if index < len(calendar) else None


def _occupation_times(
    request: SlotRequest, start: float, setup: float
) -> tuple[float, float] | None:
    """``(setup_end, end)`` from ``start``; None when the calendar does not allow it.

    For ``pause`` None means the calendar ran out; for ``forbid`` it means the occupation
    does not fit in the working run that holds ``start``.
    """
    calendar = request.calendar
    if request.shift_crossing == "pause":
        setup_end = finish_after_work(calendar, start, setup)
        end = finish_after_work(calendar, start, setup + request.run_seconds)
        if setup_end is None or end is None:
            return None
        return setup_end, end
    end = start + setup + request.run_seconds
    if not fits_within(calendar, start, end):
        return None
    return start + setup, end


def _gate_floor(request: SlotRequest, setup: float) -> float:
    """Lowest start the material gate allows; a detached setup may end exactly at it."""
    gate = request.gate_ready - setup if request.detached else request.gate_ready
    return max(request.earliest_start, gate)


def _fit_between(request: SlotRequest, setup: float, lo: float, hi: float) -> Slot | None:
    """Earliest slot with ``start >= lo`` and ``end <= hi``.

    Calendars only delay work, so ``setup_end >= start + setup`` and a detached gate met
    by ``_gate_floor`` stays met.
    """
    candidate: float | None = max(lo, _gate_floor(request, setup))
    while candidate is not None:
        start = next_working(request.calendar, candidate)
        if start is None:
            return None
        times = _occupation_times(request, start, setup)
        if times is None:
            if request.shift_crossing == "pause":
                return None
            candidate = _next_interval_start(request.calendar, start)
            continue
        setup_end, end = times
        if end > hi:
            return None
        return Slot(start=start, setup_end=setup_end, end=end, setup_seconds=setup)
    return None


class MachineTimeline:
    """Placed occupations of one machine, sorted by start (see the module docstring).

    ``available_from`` is the machine state's value; ``batch_capacity`` is set for batch
    machines only. Setups come from ``setups.seconds(machine_id, prev, next)``.
    """

    __slots__ = (
        "_available_from",
        "_batch_by_id",
        "_capacity",
        "_occupations",
        "_setups",
        "_starts",
        "machine_id",
    )

    def __init__(
        self,
        machine_id: str,
        setups: SetupTable,
        *,
        available_from: float,
        batch_capacity: float | None = None,
    ) -> None:
        self.machine_id = machine_id
        self._setups = setups
        self._available_from = available_from
        self._capacity = batch_capacity
        self._occupations: list[_Occupation] = []
        self._starts: list[float] = []
        self._batch_by_id: dict[str, _Occupation] = {}

    def find_slot(self, request: SlotRequest, accept: SlotFilter | None = None) -> Slot | None:
        """Earliest slot for ``request`` that ``accept`` (if given) accepts; None if none.

        On a batch machine an open batch the operation can join wins over a new slot.
        Raises ValueError when ``accept`` returns a retry that does not move forward.
        """
        if self._batches(request):
            joined = self._join_slot(request, accept)
            if joined is not None:
                return joined
        return self._search(request, accept)

    def place(self, request: SlotRequest, slot: Slot) -> str | None:
        """Record ``slot`` for ``request``; returns its batch id (None when not batched).

        Use slots from ``find_slot``, or a hand-built slot for an in-progress operation.
        Raises ValueError when the slot overlaps an occupation or overfills its batch.
        """
        if slot.batch_id is not None:
            return self._join(request, slot)
        index = self._free_index(slot)
        batch_id = None
        if self._batches(request):
            batch_id = f"{self.machine_id}#b{len(self._batch_by_id) + 1}"
        occupation = _Occupation(
            members=[request.op_id],
            start=slot.start,
            setup_end=slot.setup_end,
            end=slot.end,
            setup_seconds=slot.setup_seconds,
            batch_id=batch_id,
            batch_family=request.batch_family if batch_id else None,
            quantity=request.quantity,
        )
        self._occupations.insert(index, occupation)
        self._starts.insert(index, slot.start)
        if batch_id is not None:
            self._batch_by_id[batch_id] = occupation
        return batch_id

    # --- search ------------------------------------------------------------------------

    def _search(self, request: SlotRequest, accept: SlotFilter | None) -> Slot | None:
        lower = request.earliest_start
        while True:
            slot = self._earliest_slot(request, lower)
            if slot is None or accept is None:
                return slot
            retry = accept(slot)
            if retry is None:
                return slot
            if math.isinf(retry):
                return None
            if retry <= slot.start:
                raise ValueError(f"slot filter retry {retry} does not pass start {slot.start}")
            lower = retry

    def _earliest_slot(self, request: SlotRequest, lower: float) -> Slot | None:
        floor = max(lower, self._available_from)
        for index in range(bisect_right(self._starts, floor), len(self._occupations) + 1):
            slot = self._fit_gap(request, index, floor)
            if slot is not None:
                return slot
        return None

    def _fit_gap(self, request: SlotRequest, index: int, floor: float) -> Slot | None:
        """Earliest slot between occupations ``index - 1`` and ``index``."""
        previous = self._occupations[index - 1] if index > 0 else None
        follower = self._occupations[index] if index < len(self._occupations) else None
        setup = self._setup_after(previous, request.op_id)
        if setup is None:
            return None
        if follower is not None and not self._keeps_setup(request.op_id, follower):
            return None
        lo = max(floor, previous.end) if previous else floor
        hi = follower.start if follower else math.inf
        return _fit_between(request, setup, lo, hi)

    def _setup_after(self, previous: _Occupation | None, op_id: str) -> float | None:
        """Setup before ``op_id``; None when batch members would disagree on it."""
        predecessors: Sequence[str | None] = previous.members if previous else (None,)
        values = {self._setups.seconds(self.machine_id, p, op_id) for p in predecessors}
        return values.pop() if len(values) == 1 else None

    def _keeps_setup(self, op_id: str, follower: _Occupation) -> bool:
        """True when ``op_id`` as predecessor leaves the follower's setup unchanged."""
        return all(
            self._setups.seconds(self.machine_id, op_id, member) == follower.setup_seconds
            for member in follower.members
        )

    def _free_index(self, slot: Slot) -> int:
        """Insert position of ``slot``; raises ValueError when it overlaps a neighbour."""
        index = bisect_right(self._starts, slot.start)
        previous = self._occupations[index - 1] if index > 0 else None
        follower = self._occupations[index] if index < len(self._occupations) else None
        if (previous and previous.end > slot.start) or (follower and slot.end > follower.start):
            raise ValueError(
                f"slot [{slot.start}, {slot.end}) overlaps an occupation on {self.machine_id}"
            )
        return index

    # --- batches -----------------------------------------------------------------------

    def _batches(self, request: SlotRequest) -> bool:
        return self._capacity is not None and request.batch_family is not None

    def _join_slot(self, request: SlotRequest, accept: SlotFilter | None) -> Slot | None:
        for index, occupation in enumerate(self._occupations):
            if not self._can_join(request, index, occupation):
                continue
            slot = Slot(
                start=occupation.start,
                setup_end=occupation.setup_end,
                end=occupation.end,
                setup_seconds=occupation.setup_seconds,
                batch_id=occupation.batch_id,
            )
            if accept is None or accept(slot) is None:
                return slot
        return None

    def _can_join(self, request: SlotRequest, index: int, occupation: _Occupation) -> bool:
        return (
            occupation.batch_family == request.batch_family
            and self._has_room(occupation, request.quantity)
            and self._ready_at(request, occupation)
            and self._same_setups_as_members(request.op_id, index, occupation)
            and _occupation_times(request, occupation.start, occupation.setup_seconds)
            == (occupation.setup_end, occupation.end)
        )

    def _has_room(self, occupation: _Occupation, quantity: float) -> bool:
        return self._capacity is not None and occupation.quantity + quantity <= self._capacity

    def _ready_at(self, request: SlotRequest, occupation: _Occupation) -> bool:
        gate = occupation.setup_end if request.detached else occupation.start
        return occupation.start >= request.earliest_start and gate >= request.gate_ready

    def _same_setups_as_members(self, op_id: str, index: int, occupation: _Occupation) -> bool:
        previous = self._occupations[index - 1] if index > 0 else None
        follower = self._occupations[index + 1] if index + 1 < len(self._occupations) else None
        if self._setup_after(previous, op_id) != occupation.setup_seconds:
            return False
        return follower is None or self._keeps_setup(op_id, follower)

    def _join(self, request: SlotRequest, slot: Slot) -> str:
        batch_id = slot.batch_id
        occupation = self._batch_by_id.get(batch_id) if batch_id else None
        if batch_id is None or occupation is None:
            raise ValueError(f"unknown batch {batch_id} on {self.machine_id}")
        if not self._has_room(occupation, request.quantity):
            raise ValueError(f"batch {batch_id} would exceed its capacity")
        if (slot.start, slot.setup_end, slot.end) != (
            occupation.start,
            occupation.setup_end,
            occupation.end,
        ):
            raise ValueError(f"slot times differ from batch {batch_id}")
        occupation.members.append(request.op_id)
        occupation.quantity += request.quantity
        return batch_id


# --- operator pools ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PoolNeed:
    """Plain copy of an ``OperatorNeed``: ``quantity`` operators of a pool for a phase."""

    pool_id: str
    quantity: int
    phase: Phase


_Window: TypeAlias = tuple[float, float, int]


class PoolTimeline:
    """Load of one operator pool over time, as a step function (D-063).

    ``calendar`` is the pool's working intervals; it is enforced through the operation's
    effective calendar (``effective_calendar``), not here. Each lookup costs
    O(log n + k) in the breakpoints it crosses; ``hold`` costs O(n) list inserts.
    """

    __slots__ = ("_loads", "_points", "calendar", "pool_id", "size")

    def __init__(self, pool_id: str, size: int, calendar: Intervals = None) -> None:
        self.pool_id = pool_id
        self.size = size
        self.calendar = calendar
        # Load on [_points[i], _points[i + 1]); the last segment runs to +inf.
        self._points: list[float] = [-math.inf]
        self._loads: list[int] = [0]

    def blocked(self, start: float, end: float, quantity: int) -> float | None:
        """None when ``quantity`` more fits on ``[start, end)``; else when it clears.

        The returned time ends the first over-capacity run that touches the window, so a
        window must start there or later to avoid it; inf when ``quantity > size``.
        """
        if end <= start:
            return None
        limit = self.size - quantity
        if limit < 0:
            return math.inf
        index = bisect_right(self._points, start) - 1
        while index < len(self._points) and self._points[index] < end:
            if self._loads[index] > limit:
                return self._clears_at(index, limit)
            index += 1
        return None

    def hold(self, start: float, end: float, quantity: int) -> None:
        """Add ``quantity`` to the load on ``[start, end)``; ValueError on overload."""
        if self.blocked(start, end, quantity) is not None:
            raise ValueError(f"pool {self.pool_id} is over capacity on [{start}, {end})")
        if end <= start:
            return
        first = self._split(start)
        last = self._split(end)
        for index in range(first, last):
            self._loads[index] += quantity

    def _clears_at(self, index: int, limit: int) -> float:
        # The last segment always has load 0 <= limit, so the walk stops.
        while self._loads[index] > limit:
            index += 1
        return self._points[index]

    def _split(self, t: float) -> int:
        """Index of the segment starting at ``t``, inserting a breakpoint if needed."""
        index = bisect_right(self._points, t) - 1
        if self._points[index] == t:
            return index
        self._points.insert(index + 1, t)
        self._loads.insert(index + 1, self._loads[index])
        return index + 1


def _pool_windows(slot: Slot, needs: Iterable[PoolNeed]) -> list[tuple[str, _Window]]:
    """Per pool, the setup and run windows with their summed quantities."""
    setup: dict[str, int] = {}
    run: dict[str, int] = {}
    for need in needs:
        if need.phase != "run":
            setup[need.pool_id] = setup.get(need.pool_id, 0) + need.quantity
        if need.phase != "setup":
            run[need.pool_id] = run.get(need.pool_id, 0) + need.quantity
    windows: list[tuple[str, _Window]] = []
    for pool_id in sorted(setup.keys() | run.keys()):
        windows.append((pool_id, (slot.start, slot.setup_end, setup.get(pool_id, 0))))
        windows.append((pool_id, (slot.setup_end, slot.end, run.get(pool_id, 0))))
    return windows


def operator_retry(
    slot: Slot, needs: Iterable[PoolNeed], pools: Mapping[str, PoolTimeline]
) -> float | None:
    """``find_slot`` filter: None when every need fits, else the start to retry from.

    Each blocked window must start where its overload clears; the retry shifts the slot by
    the largest such distance (a lower bound when phase offsets stay the same).
    """
    retry: float | None = None
    for pool_id, (start, end, quantity) in _pool_windows(slot, needs):
        clears = pools[pool_id].blocked(start, end, quantity)
        if clears is not None:
            candidate = slot.start + (clears - start)
            retry = candidate if retry is None else max(retry, candidate)
    return retry


def hold_operators(
    slot: Slot, needs: Iterable[PoolNeed], pools: Mapping[str, PoolTimeline]
) -> None:
    """Hold every need of a placed slot; ValueError (and nothing held) on any overload."""
    windows = _pool_windows(slot, needs)
    for pool_id, window in windows:
        if pools[pool_id].blocked(*window) is not None:
            raise ValueError(f"pool {pool_id} is over capacity on [{window[0]}, {window[1]})")
    for pool_id, window in windows:
        pools[pool_id].hold(*window)


def effective_calendar(
    machine_calendar: Intervals, pool_calendars: Iterable[Intervals]
) -> Intervals:
    """The machine calendar intersected with every pool calendar (SDD Data Model)."""
    calendar = machine_calendar
    for pool_calendar in pool_calendars:
        calendar = intersect(calendar, pool_calendar)
    return calendar
