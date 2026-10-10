"""Calendar expansion: ERP plant-local windows to TF-002 seconds-from-`as_of` (COMP-114).

Working time is a set of plant-local WALL-CLOCK minutes, localized with `zoneinfo` at
`fold=0` (REQ-158):

* a local date uses its exception's windows when one exists (empty = closed), else its
  weekday's windows (absent weekday = closed);
* a window with `end_minute <= start_minute` runs on into the next local date, so the walk
  starts one day before `as_of` to catch the overnight carry-in;
* spring-forward gap minutes do not exist and are skipped (a window starting in the gap
  starts at the gap end);
* fall-back: the repeated hour counts once, as its first (`fold=0`) occurrence; its second
  occurrence is not working time, so an overnight window spanning it has a one-hour hole.

The cost is O(days x windows): each wall-clock range is cut only where its UTC offset or
its existence changes, and each constant piece maps to one real-time interval.

The day walk (`_dated_windows`) is generic over any window with `start_minute` and
`end_minute`, so overtime expansion (COMP-126) can reuse it with its own window type.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Sequence
from typing import Protocol, TypeVar
from zoneinfo import ZoneInfo

from twinflow.erp.schema import MINUTES_PER_DAY, Calendar, TimeWindow
from twinflow.scheduling.model import Interval

__all__ = ["expand"]

_SECONDS_PER_MINUTE = 60.0
_ONE_DAY = dt.timedelta(days=1)


class _MinuteWindow(Protocol):
    """Any plant-local window of minutes (regular time or overtime)."""

    @property
    def start_minute(self) -> int: ...

    @property
    def end_minute(self) -> int: ...


W = TypeVar("W", bound=_MinuteWindow)

# A wall-clock minute's localization: (UTC offset seconds at fold=0, the minute exists).
_Key = tuple[float, bool]


def expand(
    calendar: Calendar, tz: ZoneInfo, as_of: dt.datetime, horizon_end: dt.datetime
) -> tuple[Interval, ...]:
    """Return the calendar's working time as sorted, merged seconds in `[0, horizon]`.

    `as_of` and `horizon_end` are aware instants in any zone; the result is real elapsed
    seconds from `as_of`, clipped at `horizon = horizon_end - as_of`, ready for a TF-002
    `Calendar.intervals`.
    """
    week = {day.weekday: day.windows for day in calendar.week}
    exceptions = {exc.date: exc.windows for exc in calendar.exceptions}

    def windows_on(day: dt.date) -> Sequence[TimeWindow]:
        return exceptions.get(day, week.get(day.weekday(), ()))

    origin = as_of.timestamp()
    pieces = [
        (start - origin, end - origin)
        for day, window in _dated_windows(as_of, horizon_end, tz, windows_on)
        for start, end in _window_epochs(tz, day, window)
    ]
    return _clip_and_merge(pieces, horizon_end.timestamp() - origin)


def _dated_windows(
    as_of: dt.datetime,
    horizon_end: dt.datetime,
    tz: ZoneInfo,
    windows_on: Callable[[dt.date], Sequence[W]],
) -> Iterator[tuple[dt.date, W]]:
    """Yield `(local date, window)` from the day before `as_of` to `horizon_end`'s date."""
    day = as_of.astimezone(tz).date() - _ONE_DAY
    last = horizon_end.astimezone(tz).date()
    while day <= last:
        for window in windows_on(day):
            yield day, window
        day += _ONE_DAY


def _window_epochs(tz: ZoneInfo, day: dt.date, window: _MinuteWindow) -> Iterator[Interval]:
    """Yield the real epoch intervals of one window, splitting an overnight one at midnight."""
    start, end = window.start_minute, window.end_minute
    if end > start:
        yield from _range_epochs(tz, day, start, end)
        return
    yield from _range_epochs(tz, day, start, MINUTES_PER_DAY)
    yield from _range_epochs(tz, day + _ONE_DAY, 0, end)


def _range_epochs(tz: ZoneInfo, day: dt.date, start: int, end: int) -> Iterator[Interval]:
    """Yield epoch intervals for wall-clock minutes `[start, end)` of one local date."""
    midnight = dt.datetime.combine(day, dt.time())
    for lo, hi, (offset, exists) in _constant_pieces(tz, midnight, start, end):
        if exists:
            first = _naive_epoch(midnight, lo) - offset
            last = _naive_epoch(midnight, hi - 1) - offset
            yield first, last + _SECONDS_PER_MINUTE


def _constant_pieces(
    tz: ZoneInfo, midnight: dt.datetime, start: int, end: int
) -> list[tuple[int, int, _Key]]:
    """Cut minutes `[start, end)` into maximal runs with one localization key.

    Bisects only where the first and last minute disagree, so a day without a DST
    transition costs two localizations. Assumes no zone has two transitions inside one
    range that cancel out (no real IANA zone does within 24 hours).
    """
    first_key = _localize(tz, midnight, start)
    last_key = _localize(tz, midnight, end - 1)
    if first_key == last_key:
        return [(start, end, first_key)]
    if end - start == 2:
        return [(start, start + 1, first_key), (end - 1, end, last_key)]
    mid = (start + end) // 2
    left = _constant_pieces(tz, midnight, start, mid)
    right = _constant_pieces(tz, midnight, mid, end)
    if left[-1][2] == right[0][2]:
        joined = (left[-1][0], right[0][1], left[-1][2])
        return [*left[:-1], joined, *right[1:]]
    return [*left, *right]


def _localize(tz: ZoneInfo, midnight: dt.datetime, minute: int) -> _Key:
    """Localize one wall-clock minute at `fold=0`: its UTC offset and whether it exists."""
    naive = midnight + dt.timedelta(minutes=minute)
    epoch = naive.replace(tzinfo=tz).timestamp()
    exists = dt.datetime.fromtimestamp(epoch, tz).replace(tzinfo=None) == naive
    return _naive_epoch(midnight, minute) - epoch, exists


def _naive_epoch(midnight: dt.datetime, minute: int) -> float:
    """Epoch seconds of the wall-clock minute read as if it were UTC."""
    naive = midnight + dt.timedelta(minutes=minute)
    return naive.replace(tzinfo=dt.UTC).timestamp()


def _clip_and_merge(pieces: list[Interval], horizon: float) -> tuple[Interval, ...]:
    """Clip pieces to `[0, horizon]`, drop empty ones, sort, merge overlapping or touching."""
    merged: list[Interval] = []
    for start, end in sorted(pieces):
        lo, hi = max(start, 0.0), min(end, horizon)
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return tuple(merged)
