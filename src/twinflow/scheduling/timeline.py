"""Machine and operator-pool timelines for constructive solvers (COMP-009, REQ-075).

Plain mutable state the baseline builds once from the compiled problem; no Pydantic model
is created or validated here (SDD risk note). Times are float seconds.

``MachineTimeline`` (one per machine) holds placed occupations sorted by (start, end,
first member id), the verifier's sequence order, so an empty occupation ``[t, t)`` sits
before one that starts at ``t``; a machine is busy on ``[start, end)`` and runs one
operation or one batch at a time (D-062).
``find_slot`` returns the earliest slot that satisfies, per the SDD Data Model:

- ``start >= max(available_from, earliest_start)`` (callers fold ``now``, pins and the
  job release they want on ``start`` into ``earliest_start``);
- the material gate (D-063): attached setup ``start >= gate_ready``; detached setup
  ``setup_end >= gate_ready``, so the setup may run before the material arrives;
- the setup is ``SetupTable.seconds`` from the previous occupation by start order, or the
  machine state when the slot comes first (D-064);
- restart (D-074), when the timeline has a rule ``(idle_threshold_seconds,
  warmup_seconds)``: the setup phase is the setup plus the warm-up when the idle before
  ``start`` is strictly longer than the threshold. Idle is clock time from the end of the
  previous occupation, or from ``max(now, available_from)`` for the first. The warm-up is
  setup work: ``pause`` pauses it like the setup. It never pulls a start earlier: the
  detached-setup gate floor uses the sequence-dependent setup alone, so a warm-up only
  moves ``setup_end`` and ``end`` later;
- gap insertion never moves or reshapes a follower: a gap is used only when the
  follower's setup with the new operation as predecessor, plus the warm-up its new idle
  (follower start minus the new end) calls for, equals the follower's recorded setup.
  Every placed operation therefore keeps ``setup == SetupTable.seconds(machine,
  predecessor by start order, op) + warm-up``, which is what the verifier's ``setup`` and
  ``restart`` checks read. The check runs on the earliest slot in a gap: a later slot ends
  later, so a follower that needs to keep its warm-up is no better off; a follower that
  must stay warm-up free could be served by a later slot in the same gap, which the
  search does not try (it moves on to the next gap);
- calendar (D-065) on the operation's effective calendar (see ``effective_calendar``):
  ``forbid`` puts ``[start, end)`` inside one working run; ``pause`` sets
  ``setup_end = finish_after_work(start, setup)`` and
  ``end = finish_after_work(start, setup + run)``. Both allow ``_SLACK`` seconds of float
  noise past a working run's end, far below the verifier's tolerance.
  ``finish_unattended`` needs ``start`` in working time, then ``setup_end = start + setup``
  and ``end = setup_end + run``.

The search skips gaps too short for the run alone (setups are never negative): a NumPy
array holds the gap before each occupation, so one vectorized comparison finds the next
gap that may fit and a packed run of occupations is never visited gap by gap. ``place``
updates it with one array insert.

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

Precedence (shared by the baseline and the kernel decoder, D-071): ``material_gate`` turns
placed predecessors (``Placed``) into a ``Gate`` (lowest gate, max-lag limit, transfer-lot
end floors) and ``SlotCheck`` is the ``find_slot`` filter that enforces it together with
``pin.latest_start`` and operator capacity.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, Protocol, TypeAlias

import numpy as np
import numpy.typing as npt

from twinflow.scheduling.calendar_math import (
    Interval,
    Intervals,
    finish_after_work,
    fits_within,
    intersect,
    next_working,
)
from twinflow.scheduling.setups import SetupTable

ShiftCrossing: TypeAlias = Literal["forbid", "pause", "finish_unattended"]
Phase: TypeAlias = Literal["setup", "run", "both"]
RestartRule: TypeAlias = tuple[float, float]
"""``(idle_threshold_seconds, warmup_seconds)`` of a machine's restart rule (D-074)."""

_SLACK = 1e-9
"""Float slack past a working run's end; far below the verifier's 1e-6."""
_GAP_ROUNDING = 1e-12
"""Relative float error allowed when a gap length is compared with a run length."""


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

    ``setup_seconds`` is the setup work, warm-up included (it differs from
    ``setup_end - start`` only when a ``pause`` setup crosses a break). ``batch_id`` is set
    when the slot joins that batch.
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


@dataclass(frozen=True, slots=True)
class _Warmup:
    """Warm-up due at a start (D-074): ``seconds`` when ``start - idle_since > threshold``."""

    idle_since: float
    threshold: float = math.inf
    seconds: float = 0.0

    def at(self, start: float) -> float:
        return self.seconds if start - self.idle_since > self.threshold else 0.0


_NO_WARMUP = _Warmup(idle_since=-math.inf)


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
    does not fit in the working run that holds ``start``. ``finish_unattended`` only needs
    ``start`` in working time (the caller's ``next_working`` gives that), then runs on.
    """
    calendar = request.calendar
    if request.shift_crossing == "finish_unattended":
        return start + setup, start + setup + request.run_seconds
    if request.shift_crossing == "pause":
        setup_end = finish_after_work(calendar, start, setup, _SLACK)
        end = finish_after_work(calendar, start, setup + request.run_seconds, _SLACK)
        if setup_end is None or end is None:
            return None
        return setup_end, end
    end = start + setup + request.run_seconds
    if not fits_within(calendar, start, end, _SLACK):
        return None
    return start + setup, end


def _gate_floor(request: SlotRequest, setup: float) -> float:
    """Lowest start the material gate allows; a detached setup may end exactly at it."""
    gate = request.gate_ready - setup if request.detached else request.gate_ready
    return max(request.earliest_start, gate)


def _fit_between(
    request: SlotRequest, setup: float, lo: float, hi: float, warmup: _Warmup
) -> Slot | None:
    """Earliest slot with ``start >= lo`` and ``end <= hi``; the setup phase adds ``warmup``.

    Calendars and warm-ups only delay work, so ``setup_end >= start + setup`` and a
    detached gate met by ``_gate_floor`` stays met.
    """
    candidate: float | None = max(lo, _gate_floor(request, setup))
    while candidate is not None:
        start = next_working(request.calendar, candidate)
        if start is None:
            return None
        phase = setup + warmup.at(start)
        times = _occupation_times(request, start, phase)
        if times is None:
            if request.shift_crossing == "pause":
                return None
            candidate = _next_interval_start(request.calendar, start)
            continue
        setup_end, end = times
        if end > hi:
            return None
        return Slot(start=start, setup_end=setup_end, end=end, setup_seconds=phase)
    return None


class MachineTimeline:
    """Placed occupations of one machine, sorted by start (see the module docstring).

    ``available_from`` is the machine state's value; ``batch_capacity`` is set for batch
    machines only. Setups come from ``setups.seconds(machine_id, prev, next)``. ``restart``
    is the machine's restart rule (None: no warm-up ever); ``now`` only matters with one,
    as the first occupation's idle runs from ``max(now, available_from)``.
    """

    __slots__ = (
        "_available_from",
        "_batch_by_id",
        "_capacity",
        "_first_idle_since",
        "_gaps",
        "_keys",
        "_occupations",
        "_restart",
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
        restart: RestartRule | None = None,
        now: float = -math.inf,
    ) -> None:
        self.machine_id = machine_id
        self._setups = setups
        self._available_from = available_from
        self._capacity = batch_capacity
        self._restart = restart
        self._first_idle_since = max(now, available_from)
        self._occupations: list[_Occupation] = []
        # _gaps[i]: idle time between occupations i - 1 and i (inf before the first one).
        self._gaps: npt.NDArray[np.float64] = np.empty(0)
        self._starts: list[float] = []
        self._keys: list[tuple[float, float, str]] = []
        self._batch_by_id: dict[str, _Occupation] = {}

    def copy(self) -> MachineTimeline:
        """An independent copy for search methods that branch (beam, rollouts).

        Occupations are copied (batches grow in place); the setup table is shared, it is a
        memo of fixed values.
        """
        twin = MachineTimeline.__new__(MachineTimeline)
        twin.machine_id = self.machine_id
        twin._setups = self._setups
        twin._available_from = self._available_from
        twin._capacity = self._capacity
        twin._restart = self._restart
        twin._first_idle_since = self._first_idle_since
        twin._occupations = [replace(o, members=list(o.members)) for o in self._occupations]
        twin._gaps = self._gaps.copy()
        twin._starts = list(self._starts)
        twin._keys = list(self._keys)
        twin._batch_by_id = {o.batch_id: o for o in twin._occupations if o.batch_id is not None}
        return twin

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

    def sequence(self) -> tuple[str, ...]:
        """Placed operation ids in start order (batch members together, in join order)."""
        return tuple(op_id for occupation in self._occupations for op_id in occupation.members)

    def place(self, request: SlotRequest, slot: Slot) -> str | None:
        """Record ``slot`` for ``request``; returns its batch id (None when not batched).

        Use slots from ``find_slot``, or a hand-built slot for an in-progress operation.
        Raises ValueError when the slot overlaps an occupation or overfills its batch.
        """
        if slot.batch_id is not None:
            return self._join(request, slot)
        key = (slot.start, slot.end, request.op_id)
        index = self._free_index(slot, key)
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
        self._keys.insert(index, key)
        self._record_gaps(index)
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
        last = len(self._occupations)
        index = bisect_right(self._starts, floor)
        shortest = self._shortest_useful_gap(request.run_seconds)
        while True:
            slot = self._fit_gap(request, index, floor)
            if slot is not None or index == last:
                return slot
            index = self._next_gap(index + 1, shortest)

    def _shortest_useful_gap(self, run_seconds: float) -> float:
        """A lower bound on any gap that can hold ``run_seconds`` (setups are >= 0).

        Gap lengths are float differences, so the bound gives way by the slack plus a
        relative rounding margin on the machine's latest time.
        """
        latest = abs(self._occupations[-1].end) if self._occupations else 0.0
        return run_seconds - _SLACK - _GAP_ROUNDING * latest

    def _next_gap(self, index: int, shortest: float) -> int:
        """First position >= ``index`` whose gap is at least ``shortest``; else the open end."""
        wide = self._gaps[index:] >= shortest
        if wide.size:
            hit = int(wide.argmax())
            if wide[hit]:
                return index + hit
        return len(self._occupations)

    def _record_gaps(self, index: int) -> None:
        """Gaps around a new occupation at ``index``: before it, and before its follower."""
        self._gaps = np.insert(self._gaps, index, self._gap_before(index))
        if index + 1 < len(self._occupations):
            self._gaps[index + 1] = self._gap_before(index + 1)

    def _gap_before(self, index: int) -> float:
        if index == 0:
            return math.inf
        return self._occupations[index].start - self._occupations[index - 1].end

    def _fit_gap(self, request: SlotRequest, index: int, floor: float) -> Slot | None:
        """Earliest slot between occupations ``index - 1`` and ``index``."""
        previous = self._occupations[index - 1] if index > 0 else None
        follower = self._occupations[index] if index < len(self._occupations) else None
        setup = self._setup_after(previous, request.op_id)
        if setup is None:
            return None
        lo = max(floor, previous.end) if previous else floor
        hi = follower.start if follower else math.inf
        slot = _fit_between(request, setup, lo, hi, self._warmup(index))
        if slot is None or follower is None:
            return slot
        return slot if self._keeps_setup(request.op_id, slot.end, follower) else None

    def _warmup(self, index: int) -> _Warmup:
        """Warm-up rule for an occupation at sequence position ``index``."""
        if index == 0:
            return self._warmup_since(self._first_idle_since)
        return self._warmup_since(self._occupations[index - 1].end)

    def _warmup_since(self, idle_since: float) -> _Warmup:
        if self._restart is None:
            return _NO_WARMUP
        threshold, seconds = self._restart
        return _Warmup(idle_since, threshold, seconds)

    def _setup_after(self, previous: _Occupation | None, op_id: str) -> float | None:
        """Setup before ``op_id``; None when batch members would disagree on it."""
        predecessors: Sequence[str | None] = previous.members if previous else (None,)
        values = {self._setups.seconds(self.machine_id, p, op_id) for p in predecessors}
        return values.pop() if len(values) == 1 else None

    def _keeps_setup(self, op_id: str, end: float, follower: _Occupation) -> bool:
        """True when ``op_id`` ending at ``end`` leaves the follower's setup phase unchanged."""
        warmup = self._warmup_since(end).at(follower.start)
        return all(
            self._setups.seconds(self.machine_id, op_id, member) + warmup == follower.setup_seconds
            for member in follower.members
        )

    def _free_index(self, slot: Slot, key: tuple[float, float, str]) -> int:
        """Insert position of ``slot`` by sequence key; ValueError when it overlaps a neighbour."""
        index = bisect_right(self._keys, key)
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
        setup = self._setup_after(previous, op_id)
        warmup = self._warmup(index).at(occupation.start)
        if setup is None or setup + warmup != occupation.setup_seconds:
            return False
        return follower is None or self._keeps_setup(op_id, occupation.end, follower)

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

    def copy(self) -> PoolTimeline:
        """An independent copy of the load for search methods that branch."""
        twin = PoolTimeline(self.pool_id, self.size, self.calendar)
        twin._points = list(self._points)
        twin._loads = list(self._loads)
        return twin

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


# --- precedence gate and slot check -----------------------------------------------------------


class LagLink(Protocol):
    """A precedence link's lags, as ``material_gate`` reads them."""

    @property
    def min_lag(self) -> float: ...

    @property
    def max_lag(self) -> float | None: ...

    @property
    def overlap(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class Placed:
    """Where a predecessor ended up, with the chosen option's fixed and unit seconds."""

    setup_end: float
    end: float
    fixed: float
    unit: float

    def transfer(self, quantity: float) -> float:
        """Time the first ``quantity`` units are done: setup_end + fixed + q * unit."""
        return self.setup_end + self.fixed + quantity * self.unit


@dataclass(frozen=True, slots=True)
class Gate:
    """Bounds on an operation's material gate from its release and placed predecessors.

    ``ready`` is the lowest gate, ``limit`` the highest one the max lags allow; ``lots``
    holds (pred.end, q) per transfer-lot link for the ``end >= pred.end + q * unit`` rule.
    """

    ready: float
    limit: float
    lots: tuple[tuple[float, float], ...]


def material_gate(release: float, links: Iterable[tuple[LagLink, Placed]]) -> Gate:
    """The gate of an operation released at ``release`` with these placed predecessors.

    Without overlap g >= pred.end + min_lag; with ``overlap`` q,
    g >= pred.setup_end + pred.fixed + q * pred.unit + min_lag (SDD Data Model).
    """
    ready, limit = release, math.inf
    lots: list[tuple[float, float]] = []
    for link, pred in links:
        base = pred.end if link.overlap is None else pred.transfer(link.overlap)
        ready = max(ready, base + link.min_lag)
        if link.max_lag is not None:
            limit = min(limit, base + link.max_lag)
        if link.overlap is not None:
            lots.append((pred.end, link.overlap))
    return Gate(ready, limit, tuple(lots))


class SlotCheck:
    """``find_slot`` filter for one (operation, option): None accepts, else a retry start.

    Upper bounds (``latest_start``, the gate limit from max lags) only get worse later, so
    they reject for good (inf); ``missed_max_lag`` records why. The transfer-lot end bound
    (with the option's ``unit`` seconds) and operator capacity ask for a later start.
    """

    __slots__ = (
        "_detached",
        "_end_floor",
        "_gate",
        "_latest",
        "_needs",
        "_pools",
        "missed_max_lag",
    )

    def __init__(
        self,
        gate: Gate,
        *,
        unit: float,
        latest_start: float | None,
        detached: bool,
        needs: Sequence[PoolNeed],
        pools: Mapping[str, PoolTimeline],
    ) -> None:
        self._gate = gate
        self._latest = latest_start
        self._detached = detached
        self._needs = needs
        self._pools = pools
        self._end_floor = max((end + q * unit for end, q in gate.lots), default=-math.inf)
        self.missed_max_lag = False

    def __call__(self, slot: Slot) -> float | None:
        if self._latest is not None and slot.start > self._latest + _SLACK:
            return math.inf
        material = slot.setup_end if self._detached else slot.start
        if material > self._gate.limit + _SLACK:
            self.missed_max_lag = True
            return math.inf
        retry: float | None = None
        if slot.end < self._end_floor - _SLACK:
            shifted = slot.start + (self._end_floor - slot.end)
            retry = max(shifted, math.nextafter(slot.start, math.inf))
        if self._needs:
            blocked = operator_retry(slot, self._needs, self._pools)
            if blocked is not None:
                retry = blocked if retry is None else max(retry, blocked)
        return retry
