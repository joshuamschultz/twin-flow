from __future__ import annotations

from datetime import date, datetime

import pytest
import simpy

from twinflow.primitives.calendar import (
    CalendarConfig,
    CalendarException,
    WorkingCalendar,
    WorkingInterval,
    parse_clock,
)
from twinflow.primitives.labor import PRIORITY_LOAD, LaborPool


def _weekday_calendar(*, monday_closed: bool = False) -> WorkingCalendar:
    exceptions = (CalendarException(date(2026, 3, 9), closed=True),) if monday_closed else ()
    return WorkingCalendar(
        CalendarConfig(
            timezone="America/Chicago",
            origin=datetime.fromisoformat("2026-03-06T15:00:00-06:00"),
            weekly=(
                WorkingInterval(frozenset(range(5)), parse_clock("07:00"), parse_clock("16:00")),
            ),
            exceptions=exceptions,
        )
    )


@pytest.mark.parametrize(
    ("monday_closed", "expected_hours"),
    # The weekend contains the spring-forward transition, so elapsed time is one
    # hour shorter than local wall-clock subtraction suggests.
    [(False, 64.0), (True, 88.0)],
)
def test_paused_work_skips_nights_weekends_and_closed_exception(
    monday_closed: bool, expected_hours: float
) -> None:
    env = simpy.Environment()
    pool = LaborPool("p", 1, frozenset({"s"}), _weekday_calendar(monday_closed=monday_closed), env)

    def operation():
        handle = yield from pool.request("s", PRIORITY_LOAD)
        yield from pool.work(2 * 3600.0, "pause")
        pool.release(handle)

    env.process(operation())
    env.run()
    assert env.now == pytest.approx(expected_hours * 3600.0)


def test_spring_forward_interval_uses_elapsed_utc_seconds() -> None:
    calendar = WorkingCalendar(
        CalendarConfig(
            timezone="America/Chicago",
            origin=datetime.fromisoformat("2026-03-08T00:00:00-06:00"),
            weekly=(WorkingInterval(frozenset({6}), parse_clock("00:00"), parse_clock("04:00")),),
        )
    )
    assert calendar.current_shift_end(0.0) == pytest.approx(3 * 3600.0)


def test_finish_unattended_waits_for_start_then_crosses_shift_end() -> None:
    env = simpy.Environment()
    calendar = WorkingCalendar(
        CalendarConfig(
            timezone="UTC",
            origin=datetime.fromisoformat("2026-03-02T06:00:00+00:00"),
            weekly=(WorkingInterval(frozenset({0}), parse_clock("07:00"), parse_clock("08:00")),),
        )
    )
    pool = LaborPool("p", 1, frozenset({"s"}), calendar, env)

    def operation():
        handle = yield from pool.request("s", PRIORITY_LOAD)
        yield from pool.work(2 * 3600.0, "finish_unattended")
        pool.release(handle)

    env.process(operation())
    env.run()
    assert env.now == pytest.approx(3 * 3600.0)
