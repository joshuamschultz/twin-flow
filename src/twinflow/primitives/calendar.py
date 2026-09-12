"""Timezone-aware recurring working calendars for simulated labor availability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_DAY_INDEX = {
    name: index for index, name in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
}


@dataclass(frozen=True, slots=True)
class WorkingInterval:
    """A local recurring interval on one or more weekdays."""

    days: frozenset[int]
    start: time
    end: time


@dataclass(frozen=True, slots=True)
class CalendarException:
    """A closed date or replacement local working interval."""

    day: date
    closed: bool
    start: time | None = None
    end: time | None = None


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """Compiled calendar configuration carried from model to runtime."""

    timezone: str
    origin: datetime
    weekly: tuple[WorkingInterval, ...]
    exceptions: tuple[CalendarException, ...] = ()


def parse_clock(value: str) -> time:
    """Parse a strict 24-hour `HH:MM` local wall-clock value."""
    return datetime.strptime(value, "%H:%M").time()


def parse_day(value: str) -> int:
    """Return Python weekday index for a three-letter lowercase day."""
    try:
        return _DAY_INDEX[value.lower()]
    except KeyError as exc:
        raise ValueError(f"unknown weekday {value!r}") from exc


class WorkingCalendar:
    """Map simulated seconds to timezone-aware working intervals."""

    def __init__(self, config: CalendarConfig) -> None:
        self.config = config
        self._zone = ZoneInfo(config.timezone)
        self._origin = config.origin.astimezone(self._zone)
        self._exceptions = {item.day: item for item in config.exceptions}

    def local_datetime(self, simulation_time: float) -> datetime:
        """Convert elapsed simulation seconds to local calendar time."""
        return (
            self._origin.astimezone(ZoneInfo("UTC")) + timedelta(seconds=simulation_time)
        ).astimezone(self._zone)

    def simulation_time(self, instant: datetime) -> float:
        """Convert an aware instant to elapsed simulation seconds."""
        return (
            instant.astimezone(ZoneInfo("UTC")) - self._origin.astimezone(ZoneInfo("UTC"))
        ).total_seconds()

    def is_on_shift(self, t: float) -> bool:
        """Whether `t` falls within a configured local working interval."""
        instant = self.local_datetime(t)
        return any(
            start <= instant < end for start, end in self._covering_intervals(instant.date())
        )

    def next_shift_start(self, t: float) -> float:
        """First working instant at or after `t`, searching one calendar year."""
        instant = self.local_datetime(t)
        for offset in range(367):
            day = instant.date() + timedelta(days=offset)
            for start, end in self._covering_intervals(day):
                if end <= instant:
                    continue
                candidate = max(start, instant)
                return self.simulation_time(candidate)
        raise ValueError("calendar has no working interval within 367 days")

    def current_shift_end(self, t: float) -> float:
        """End of the interval containing `t`. Raises when off shift."""
        instant = self.local_datetime(t)
        for start, end in self._covering_intervals(instant.date()):
            if start <= instant < end:
                return self.simulation_time(end)
        raise ValueError(f"simulation time {t} is outside a working interval")

    def _covering_intervals(self, day: date) -> list[tuple[datetime, datetime]]:
        """Intervals starting on `day` plus an overnight interval from the day before."""
        previous = self._intervals_for_date(day - timedelta(days=1))
        current = self._intervals_for_date(day)
        return previous + current

    def _intervals_for_date(self, day: date) -> list[tuple[datetime, datetime]]:
        exception = self._exceptions.get(day)
        if exception is not None:
            if exception.closed:
                return []
            if exception.start is None or exception.end is None:
                return []
            specs = [(exception.start, exception.end)]
        else:
            specs = [
                (item.start, item.end) for item in self.config.weekly if day.weekday() in item.days
            ]
        intervals: list[tuple[datetime, datetime]] = []
        for start_clock, end_clock in specs:
            start = datetime.combine(day, start_clock, self._zone)
            end_day = day + timedelta(days=1) if end_clock <= start_clock else day
            end = datetime.combine(end_day, end_clock, self._zone)
            intervals.append((start, end))
        return intervals


class AlwaysWorkingCalendar:
    """Compatibility calendar for models without a declared schedule."""

    def is_on_shift(self, t: float) -> bool:
        return True

    def next_shift_start(self, t: float) -> float:
        return t

    def current_shift_end(self, t: float) -> float:
        return float("inf")
