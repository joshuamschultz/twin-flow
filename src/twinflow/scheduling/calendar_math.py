"""Pure working-time arithmetic over calendar intervals (COMP-016).

A calendar is a sorted sequence of non-overlapping half-open working intervals
``[start, end)`` with ``start < end`` (the shape of ``Calendar.intervals``). ``None``
means *always working*: a machine or operator pool with no calendar never stops.
An instant ``t`` is working when some interval has ``start <= t < end``; an interval
end is therefore not a working instant. Intervals that touch (one ends where the next
starts) form one continuous working run with no break between them.

Lookups use :mod:`bisect`, so locating ``t`` costs O(log n). Functions that walk
forward (``work_seconds``, ``finish_after_work``, ``fits_within``) then cost O(k) in
the number of intervals they actually cross. No function keeps state.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from typing import TypeAlias

Interval: TypeAlias = tuple[float, float]
Intervals: TypeAlias = Sequence[Interval] | None


def _start(interval: Interval) -> float:
    return interval[0]


def _end(interval: Interval) -> float:
    return interval[1]


def _check_range(start: float, end: float) -> None:
    if end < start:
        raise ValueError(f"range end {end} is before start {start}")


def _containing(intervals: Sequence[Interval], t: float) -> int | None:
    """Index of the interval with ``start <= t < end``, or None when ``t`` is not working."""
    index = bisect_right(intervals, t, key=_start) - 1
    if index >= 0 and t < intervals[index][1]:
        return index
    return None


def _first_ending_after(intervals: Sequence[Interval], t: float) -> int:
    """Index of the first interval whose end is after ``t`` (``len`` when none)."""
    return bisect_right(intervals, t, key=_end)


def next_working(intervals: Intervals, t: float) -> float | None:
    """Earliest working instant at or after ``t``; None when no working time remains."""
    if intervals is None:
        return t
    index = _first_ending_after(intervals, t)
    if index == len(intervals):
        return None
    return max(t, intervals[index][0])


def fits_within(intervals: Intervals, start: float, end: float) -> bool:
    """True when ``[start, end)`` holds no non-working time (the ``forbid`` rule).

    The range may span touching intervals, since no break separates them. A
    zero-length range fits exactly when ``start`` is a working instant, so it never
    fits at an interval end or inside a break.
    """
    _check_range(start, end)
    if intervals is None:
        return True
    index = _containing(intervals, start)
    if index is None:
        return False
    reach = intervals[index][1]
    while reach < end and index + 1 < len(intervals) and intervals[index + 1][0] == reach:
        index += 1
        reach = intervals[index][1]
    return end <= reach


def work_seconds(intervals: Intervals, start: float, end: float) -> float:
    """Working seconds inside ``[start, end)``."""
    _check_range(start, end)
    if intervals is None:
        return end - start
    total = 0.0
    index = _first_ending_after(intervals, start)
    while index < len(intervals) and intervals[index][0] < end:
        lo, hi = intervals[index]
        total += min(hi, end) - max(lo, start)
        index += 1
    return total


def finish_after_work(intervals: Intervals, start: float, seconds: float) -> float | None:
    """Earliest ``end`` with ``work_seconds(start, end) == seconds`` (the ``pause`` rule).

    Returns None when the calendar runs out before ``seconds`` of work is done. When
    ``seconds`` is 0 the result is ``start`` itself, even inside a break: zero work
    takes no time, so ``start`` is not moved to the next working instant. Callers that
    need a working start call :func:`next_working` first. When the work ends exactly
    on an interval end, that end is returned, not the next interval's start.
    """
    if seconds < 0:
        raise ValueError(f"work seconds {seconds} is negative")
    if seconds == 0:
        return start
    if intervals is None:
        return start + seconds
    remaining = seconds
    for index in range(_first_ending_after(intervals, start), len(intervals)):
        lo, hi = intervals[index]
        begin = max(lo, start)
        if remaining <= hi - begin:
            return begin + remaining
        remaining -= hi - begin
    return None


def intersect(a: Intervals, b: Intervals) -> Intervals:
    """Working intervals common to ``a`` and ``b``; None (always working) is the identity.

    An operation's effective calendar is the intersection of its machine calendar and
    the calendars of its operator pools (SDD Data Model). Runs in O(len(a) + len(b)).
    """
    if a is None:
        return b
    if b is None:
        return a
    result: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            result.append((lo, hi))
        if a[i][1] <= b[j][1]:
            i += 1
        else:
            j += 1
    return result
