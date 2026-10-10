"""Calendar expansion checked against a minute-by-minute oracle (T-197, COMP-114).

Requirement: REQ-158.

Semantics pinned here (the implementation follows these tests):

* A local date uses its exception's windows when an exception exists for that date (an
  empty exception means closed), else its weekday's windows (a weekday absent = closed).
* A window `(start_minute, end_minute)` with `end_minute <= start_minute` runs from
  `start_minute` on its own date to `end_minute` on the next local date.
* Working time is a set of local WALL-CLOCK minutes. Each wall-clock minute is localized
  with `zoneinfo` at `fold=0`:
  - spring-forward: wall-clock minutes inside the gap do not exist and are skipped, so a
    window starting in the gap starts at the gap end;
  - fall-back: the repeated hour is counted once, as its first (`fold=0`, daylight)
    occurrence; the second (`fold=1`) occurrence is NOT working time. An overnight window
    that crosses the fall-back hour is therefore measured by its wall-clock minutes, not by
    the real time between its localized endpoints: a 22:00-06:00 shift on the fall-back night
    yields 8 working hours with a one-hour hole, not 9 continuous hours. The same holds for
    a full-day (0, 1440) window: a calendar day is 24 working hours even when it is 25 long.
* Each working minute becomes `[t, t + 60)` in seconds from `as_of` (real elapsed time),
  clipped to `[0, horizon]` with `horizon = horizon_end - as_of`; the pieces are sorted and
  touching pieces merged.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from functools import cache
from zoneinfo import ZoneInfo

import pytest

from twinflow.erp.calendars import expand
from twinflow.erp.schema import (
    Calendar,
    CalendarException,
    Lineage,
    TimeWindow,
    WeekdayWindows,
)
from twinflow.scheduling.model import Calendar as SchedulingCalendar

CHICAGO = ZoneInfo("America/Chicago")
MINUTES_PER_DAY = 1440
SEEDS = range(50)

Interval = tuple[float, float]
Window = tuple[int, int]

# Horizon (i) contains the 2026-11-01 02:00 CDT -> 01:00 CST fall-back.
FALL_BACK = (
    datetime(2026, 10, 27, 5, 0, tzinfo=CHICAGO),
    datetime(2026, 11, 6, 14, 30, tzinfo=CHICAGO),
)
# Horizon (ii) contains the 2027-03-14 02:00 CST -> 03:00 CDT spring-forward.
SPRING_FORWARD = (
    datetime(2027, 3, 9, 5, 0, tzinfo=CHICAGO),
    datetime(2027, 3, 19, 14, 30, tzinfo=CHICAGO),
)


# --- oracle ---------------------------------------------------------------------------------


@cache
def _epoch_of_wall_minute(day: date, minute: int) -> float | None:
    """Epoch seconds of local wall-clock `minute` on `day` at fold=0; None inside a gap."""
    local = datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=CHICAGO)
    epoch = local.timestamp()
    round_trip = datetime.fromtimestamp(epoch, CHICAGO).replace(tzinfo=None)
    if round_trip != local.replace(tzinfo=None):
        return None  # spring-forward gap: this wall-clock minute does not exist
    return epoch


def _windows_on(calendar: Calendar, day: date) -> Sequence[TimeWindow]:
    for exception in calendar.exceptions:
        if exception.date == day:
            return exception.windows
    return [
        w
        for week_day in calendar.week
        if week_day.weekday == day.weekday()
        for w in week_day.windows
    ]


def _working_wall_minutes(calendar: Calendar, first: date, last: date) -> set[tuple[date, int]]:
    minutes: set[tuple[date, int]] = set()
    day = first
    while day <= last:
        for window in _windows_on(calendar, day):
            start, end = window.start_minute, window.end_minute
            if end > start:
                minutes.update((day, m) for m in range(start, end))
            else:  # overnight: runs into the next local date
                minutes.update((day, m) for m in range(start, MINUTES_PER_DAY))
                minutes.update((day + timedelta(days=1), m) for m in range(0, end))
        day += timedelta(days=1)
    return minutes


def oracle(calendar: Calendar, as_of: datetime, horizon_end: datetime) -> tuple[Interval, ...]:
    """Walk every local wall-clock minute; this is the definition the expander must meet."""
    origin = as_of.timestamp()
    horizon = horizon_end.timestamp() - origin
    first = as_of.astimezone(CHICAGO).date() - timedelta(days=1)  # catch overnight carry-in
    last = horizon_end.astimezone(CHICAGO).date()
    starts = sorted(
        epoch - origin
        for day, minute in _working_wall_minutes(calendar, first, last)
        if (epoch := _epoch_of_wall_minute(day, minute)) is not None
    )
    merged: list[Interval] = []
    for start in starts:
        lo, hi = max(start, 0.0), min(start + 60.0, horizon)
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return tuple(merged)


# --- builders -------------------------------------------------------------------------------


def _lineage(cal_id: str) -> Lineage:
    return Lineage(table="calendars", key=(cal_id,))


def make_calendar(
    week: dict[int, Sequence[Window]],
    exceptions: dict[date, Sequence[Window]] | None = None,
    cal_id: str = "cal",
) -> Calendar:
    return Calendar(
        id=cal_id,
        week=tuple(
            WeekdayWindows(
                weekday=weekday,
                windows=tuple(TimeWindow(start_minute=s, end_minute=e) for s, e in windows),
            )
            for weekday, windows in sorted(week.items())
        ),
        exceptions=tuple(
            CalendarException(
                date=day,
                windows=tuple(TimeWindow(start_minute=s, end_minute=e) for s, e in windows),
            )
            for day, windows in sorted((exceptions or {}).items())
        ),
        lineage=_lineage(cal_id),
    )


def every_day(*windows: Window) -> dict[int, Sequence[Window]]:
    return {weekday: windows for weekday in range(7)}


def _random_day_windows(rng: random.Random) -> list[Window]:
    """1-3 disjoint windows; sometimes the last one is overnight, sometimes a full day."""
    if rng.random() < 0.1:
        return [(0, MINUTES_PER_DAY)]
    count = rng.randint(1, 3)
    points = sorted(rng.sample(range(0, MINUTES_PER_DAY + 1), 2 * count))
    windows = [(points[2 * i], points[2 * i + 1]) for i in range(count)]
    last_start = windows[-1][0]
    if rng.random() < 0.35 and last_start > 0:
        # Overnight: the end on the next day stays at or before today's first start.
        end = rng.randint(0, min(windows[0][0], last_start - 1))
        windows[-1] = (last_start, end)
    return windows


def random_calendar(seed: int, as_of: datetime, horizon_end: datetime) -> Calendar:
    rng = random.Random(seed)  # noqa: S311 - seeded test data, not cryptography
    week = {
        weekday: _random_day_windows(rng) for weekday in range(7) if rng.random() >= 0.2
    }  # a skipped weekday is closed
    first = as_of.astimezone(CHICAGO).date() - timedelta(days=1)
    span = (horizon_end.astimezone(CHICAGO).date() - first).days
    days = rng.sample([first + timedelta(days=d) for d in range(span + 1)], rng.randint(0, 4))
    exceptions: dict[date, Sequence[Window]] = {}
    for day in days:
        kind = rng.choice(("closed", "full", "windows"))
        if kind == "closed":
            exceptions[day] = []
        elif kind == "full":
            exceptions[day] = [(0, MINUTES_PER_DAY)]
        else:
            exceptions[day] = _random_day_windows(rng)
    return make_calendar(week, exceptions, cal_id=f"cal-{seed}")


def assert_well_formed(intervals: tuple[Interval, ...], horizon: float) -> None:
    previous_end = -1.0
    for start, end in intervals:
        assert 0.0 <= start < end <= horizon
        assert start > previous_end, "intervals must be sorted, disjoint and touching ones merged"
        previous_end = end


# --- property: expand equals the minute oracle across DST -----------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(
    ("as_of", "horizon_end"), [FALL_BACK, SPRING_FORWARD], ids=["fall-back", "spring-forward"]
)
def test_expand_matches_minute_oracle_across_dst(
    seed: int, as_of: datetime, horizon_end: datetime
) -> None:
    calendar = random_calendar(seed, as_of, horizon_end)

    result = expand(calendar, CHICAGO, as_of, horizon_end)

    assert isinstance(result, tuple)
    assert result == oracle(calendar, as_of, horizon_end)
    assert_well_formed(result, horizon_end.timestamp() - as_of.timestamp())
    # The output is a valid TF-002 calendar.
    SchedulingCalendar(id=calendar.id, intervals=result)


# --- hand-computed DST anchors (pin the semantics a reader can check) -----------------------


def test_expand_night_shift_on_fall_back_night_counts_repeated_hour_once() -> None:
    # 22:00-06:00 every day. as_of Sat 2026-10-31 12:00 CDT (17:00 UTC).
    calendar = make_calendar(every_day((1320, 360)))
    as_of = datetime(2026, 10, 31, 12, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 11, 2, 0, 0, tzinfo=CHICAGO)  # 06:00 UTC, 37 h later

    result = expand(calendar, CHICAGO, as_of, horizon_end)

    assert result == (
        (36_000.0, 50_400.0),  # 22:00 CDT .. 01:59:59 CDT (03:00..07:00 UTC)
        # 01:00-01:59 CST (fold=1, 07:00..08:00 UTC) is the repeated hour: not working.
        (54_000.0, 68_400.0),  # 02:00 CST .. 06:00 CST (08:00..12:00 UTC)
        (126_000.0, 133_200.0),  # Sun 22:00 CST .. clipped at horizon (Mon 00:00 CST)
    )
    assert result == oracle(calendar, as_of, horizon_end)


def test_expand_window_starting_in_spring_forward_gap_starts_at_gap_end() -> None:
    # Sunday 2027-03-14: 02:30-05:00, but 02:00-02:59 does not exist.
    calendar = make_calendar({6: [(150, 300)]})
    as_of = datetime(2027, 3, 14, 0, 0, tzinfo=CHICAGO)  # 06:00 UTC
    horizon_end = datetime(2027, 3, 15, 0, 0, tzinfo=CHICAGO)  # 05:00 UTC, 23 h later

    result = expand(calendar, CHICAGO, as_of, horizon_end)

    assert result == ((7_200.0, 14_400.0),)  # 03:00 CDT .. 05:00 CDT
    assert result == oracle(calendar, as_of, horizon_end)


def test_expand_window_spanning_spring_forward_gap_is_continuous_real_time() -> None:
    # 01:00-04:00 local on 2027-03-14 is 01:00 CST .. 04:00 CDT = 2 real hours, no hole.
    calendar = make_calendar({}, {date(2027, 3, 14): [(60, 240)]})
    as_of = datetime(2027, 3, 14, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2027, 3, 15, 0, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((3_600.0, 10_800.0),)


# --- exceptions -----------------------------------------------------------------------------


def test_expand_exception_replaces_weekday_windows() -> None:
    monday = date(2026, 10, 5)
    calendar = make_calendar({0: [(480, 960)]}, {monday: [(600, 660)]})
    as_of = datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 6, 0, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((36_000.0, 39_600.0),)


def test_expand_empty_exception_closes_the_day() -> None:
    monday = date(2026, 10, 5)
    calendar = make_calendar({0: [(480, 960)]}, {monday: []})
    as_of = datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 6, 0, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ()


def test_expand_exception_on_previous_day_controls_overnight_carry_in() -> None:
    # Sunday's overnight shift would run into Monday 00:00-06:00; Sunday is closed instead.
    sunday = date(2026, 10, 4)
    calendar = make_calendar(every_day((1320, 360)), {sunday: []})
    as_of = datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 5, 12, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ()


# --- merging, clipping, start inside a window -----------------------------------------------


def test_expand_full_days_merge_into_one_interval_over_horizon() -> None:
    calendar = make_calendar(every_day((0, MINUTES_PER_DAY)))
    as_of = datetime(2026, 10, 5, 7, 15, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 9, 18, 45, tzinfo=CHICAGO)
    horizon = horizon_end.timestamp() - as_of.timestamp()

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((0.0, horizon),)


def test_expand_touching_windows_across_midnight_merge() -> None:
    # Mon 16:00-24:00 then Tue 00:00-08:00 touch at midnight: one interval.
    calendar = make_calendar({0: [(960, MINUTES_PER_DAY)], 1: [(0, 480)]})
    as_of = datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 7, 0, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((57_600.0, 115_200.0),)


def test_expand_as_of_mid_window_starts_at_zero() -> None:
    calendar = make_calendar(every_day((480, 1020)))
    as_of = datetime(2026, 10, 6, 10, 17, 30, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 7, 0, 0, tzinfo=CHICAGO)

    result = expand(calendar, CHICAGO, as_of, horizon_end)

    assert result == ((0.0, 24_150.0),)  # 10:17:30 .. 17:00


def test_expand_as_of_inside_overnight_carry_in_starts_at_zero() -> None:
    calendar = make_calendar(every_day((1320, 360)))
    as_of = datetime(2026, 10, 6, 3, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 6, 12, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((0.0, 10_800.0),)


def test_expand_clips_last_interval_at_horizon() -> None:
    calendar = make_calendar(every_day((480, 1020)))
    as_of = datetime(2026, 10, 6, 0, 0, tzinfo=CHICAGO)
    horizon_end = datetime(2026, 10, 6, 12, 0, tzinfo=CHICAGO)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ((28_800.0, 43_200.0),)


def test_expand_as_of_in_utc_equals_same_instant_in_plant_time() -> None:
    calendar = random_calendar(7, *FALL_BACK)
    as_of, horizon_end = FALL_BACK

    in_plant = expand(calendar, CHICAGO, as_of, horizon_end)
    in_utc = expand(calendar, CHICAGO, as_of.astimezone(UTC), horizon_end.astimezone(UTC))

    assert in_utc == in_plant


# --- no working time ------------------------------------------------------------------------


def test_expand_empty_calendar_returns_empty_tuple() -> None:
    calendar = make_calendar({})

    assert expand(calendar, CHICAGO, *FALL_BACK) == ()


def test_expand_every_day_closed_by_exception_returns_empty_tuple() -> None:
    as_of, horizon_end = FALL_BACK
    first = as_of.date() - timedelta(days=1)
    closed = {first + timedelta(days=d): [] for d in range((horizon_end.date() - first).days + 1)}
    calendar = make_calendar(every_day((480, 1020)), closed)

    assert expand(calendar, CHICAGO, as_of, horizon_end) == ()
