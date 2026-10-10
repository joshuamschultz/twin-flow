"""Machine calendars of the problem builder, with priced overtime (COMP-114, COMP-126).

A machine uses its own calendar, else its work center's; neither means always working.

Overtime (D-125) is a priced subset of working time. With `ErpPolicy.use_overtime` true, a
machine's overtime is `machine.overtime` when set, else its work center's (the machine's
REPLACES the center's; they are never combined). An active machine with overtime and a
regular calendar `C` gets its own TF-002 calendar `f"{C}+ot:{machine_id}"` whose
`intervals` are regular time union overtime and whose `overtime` is the overtime minus
regular time, each piece keeping its multiplier. Regular calendars stay overtime-free, so
machines without overtime keep sharing them. An always-working machine has nothing to add,
so it keeps `calendar_id = None`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from zoneinfo import ZoneInfo

from twinflow.erp.calendars import OvertimePiece, expand, expand_overtime
from twinflow.erp.profile import PlanningProfile
from twinflow.erp.schema import ErpSnapshot, Machine, Overtime
from twinflow.scheduling.model import Calendar, Interval, OvertimeInterval

__all__ = ["machine_calendars"]


def machine_calendars(
    snapshot: ErpSnapshot, profile: PlanningProfile
) -> tuple[dict[str, str | None], tuple[Calendar, ...]]:
    """Each active machine's calendar id and every calendar a machine or pool uses, by id."""
    tz = ZoneInfo(snapshot.plant_timezone)
    # Real elapsed days in UTC, so the horizon never depends on how `as_of`'s zone is spelled.
    horizon_end = snapshot.as_of.astimezone(dt.UTC) + dt.timedelta(days=profile.erp.horizon_days)
    sources = {cal.id: cal for cal in reversed(snapshot.calendars)}  # first record wins
    expanded: dict[str, tuple[Interval, ...]] = {}

    def regular(calendar_id: str) -> tuple[Interval, ...]:
        if calendar_id not in expanded:
            source = sources[calendar_id]
            expanded[calendar_id] = expand(source, tz, snapshot.as_of, horizon_end)
        return expanded[calendar_id]

    regular_ids = _regular_calendar_ids(snapshot)
    assigned = dict(regular_ids)
    own: list[Calendar] = []
    overtimes = _machine_overtimes(snapshot) if profile.erp.use_overtime else {}
    for machine_id, overtime in overtimes.items():
        regular_id = regular_ids[machine_id]
        if regular_id is None:
            continue  # always working: overtime adds no working time
        pieces = expand_overtime(overtime, tz, snapshot.as_of, horizon_end)
        calendar = _with_overtime(f"{regular_id}+ot:{machine_id}", regular(regular_id), pieces)
        assigned[machine_id] = calendar.id
        own.append(calendar)
    shared = {cid for mid, cid in assigned.items() if cid is not None and cid == regular_ids[mid]}
    shared.update(pool.calendar_id for pool in snapshot.labor_pools if pool.calendar_id)
    calendars = [*own, *(Calendar(id=cid, intervals=regular(cid)) for cid in shared)]
    return assigned, tuple(sorted(calendars, key=lambda calendar: calendar.id))


def _regular_calendar_ids(snapshot: ErpSnapshot) -> dict[str, str | None]:
    """Active machine id -> its own calendar id, else its work center's, else None."""
    centers = {wc.id: wc.calendar_id for wc in snapshot.work_centers}
    return {
        machine.id: machine.calendar_id or centers.get(machine.work_center_id)
        for machine in snapshot.machines
        if machine.active
    }


def _machine_overtimes(snapshot: ErpSnapshot) -> dict[str, Overtime]:
    """Active machine id -> its overtime (machine's replaces its center's), when it has one."""
    centers = {wc.id: wc.overtime for wc in snapshot.work_centers}
    found: dict[str, Overtime] = {}
    for machine in snapshot.machines:
        overtime = _overtime_of(machine, centers)
        if machine.active and overtime is not None:
            found[machine.id] = overtime
    return found


def _overtime_of(machine: Machine, centers: dict[str, Overtime | None]) -> Overtime | None:
    if machine.overtime is not None:
        return machine.overtime
    return centers.get(machine.work_center_id)


def _with_overtime(
    calendar_id: str, regular: Sequence[Interval], pieces: Sequence[OvertimePiece]
) -> Calendar:
    """The machine calendar: regular union overtime, priced at overtime minus regular."""
    return Calendar(
        id=calendar_id,
        intervals=_union(regular, [(start, end) for start, end, _ in pieces]),
        overtime=tuple(
            OvertimeInterval(start=start, end=end, cost_multiplier=multiplier)
            for start, end, multiplier in _minus(pieces, regular)
        ),
    )


def _union(first: Sequence[Interval], second: Sequence[Interval]) -> tuple[Interval, ...]:
    """Sorted union of two interval sets; overlapping or touching intervals merge."""
    merged: list[Interval] = []
    for start, end in sorted([*first, *second]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _minus(pieces: Sequence[OvertimePiece], regular: Sequence[Interval]) -> list[OvertimePiece]:
    """Overtime pieces with regular time cut out; each remainder keeps its multiplier.

    Both inputs are sorted and disjoint, so one forward cursor over `regular` serves all
    pieces: O(pieces + regular).
    """
    result: list[OvertimePiece] = []
    index = 0
    for start, end, multiplier in pieces:
        while index < len(regular) and regular[index][1] <= start:
            index += 1
        cursor = start
        scan = index
        while scan < len(regular) and regular[scan][0] < end:
            if regular[scan][0] > cursor:
                result.append((cursor, regular[scan][0], multiplier))
            cursor = max(cursor, regular[scan][1])
            scan += 1
        if cursor < end:
            result.append((cursor, end, multiplier))
    return result
