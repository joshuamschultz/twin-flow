"""Calendar math (T-087, COMP-016) checked against second-by-second integration.

Every random instance uses integer interval bounds inside [0, HORIZON), so the working
time in [a, b) equals the count of integer seconds t in [a, b) that lie in some
half-open working interval [s, e). The brute-force helpers below compute exactly that.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from twinflow.scheduling.calendar_math import (
    Intervals,
    finish_after_work,
    fits_within,
    intersect,
    next_working,
    work_seconds,
)

HORIZON = 60
SEEDS = range(100)

Interval = tuple[float, float]


def random_intervals(rng: np.random.Generator) -> list[Interval]:
    """Sorted, non-overlapping integer intervals; some touch end-to-start."""
    intervals: list[Interval] = []
    cursor = int(rng.integers(0, 6))
    while cursor < HORIZON - 1 and len(intervals) < 8:
        end = min(HORIZON, cursor + int(rng.integers(1, 9)))
        intervals.append((float(cursor), float(end)))
        cursor = end + int(rng.choice((0, 0, 1, 2, 5)))
    return intervals


def is_working(intervals: Sequence[Interval], t: int) -> bool:
    return any(s <= t < e for s, e in intervals)


def brute_work(intervals: Sequence[Interval], start: int, end: int) -> float:
    return float(sum(is_working(intervals, t) for t in range(start, end)))


def brute_fits(intervals: Sequence[Interval], start: int, end: int) -> bool:
    if start == end:
        return is_working(intervals, start)
    return all(is_working(intervals, t) for t in range(start, end))


def brute_finish(intervals: Sequence[Interval], start: int, seconds: int) -> float | None:
    for end in range(start, HORIZON + 1):
        if brute_work(intervals, start, end) == seconds:
            return float(end)
    return None


def brute_next(intervals: Sequence[Interval], t: int) -> float | None:
    for candidate in range(t, HORIZON + 1):
        if is_working(intervals, candidate):
            return float(candidate)
    return None


def working_set(intervals: Sequence[Interval]) -> set[int]:
    return {t for t in range(HORIZON + 1) if is_working(intervals, t)}


def sample_pairs(rng: np.random.Generator, count: int) -> list[tuple[int, int]]:
    pairs = []
    for _ in range(count):
        a, b = (int(x) for x in rng.integers(0, HORIZON + 1, size=2))
        pairs.append((min(a, b), max(a, b)))
    return pairs


@pytest.mark.parametrize("seed", SEEDS)
def test_work_seconds_equals_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    intervals = random_intervals(rng)
    for start, end in sample_pairs(rng, 40):
        assert work_seconds(intervals, start, end) == brute_work(intervals, start, end)


@pytest.mark.parametrize("seed", SEEDS)
def test_fits_within_equals_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    intervals = random_intervals(rng)
    for start, end in sample_pairs(rng, 40):
        assert fits_within(intervals, start, end) == brute_fits(intervals, start, end)


@pytest.mark.parametrize("seed", SEEDS)
def test_finish_after_work_equals_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    intervals = random_intervals(rng)
    for _ in range(40):
        start, seconds = int(rng.integers(0, HORIZON + 1)), int(rng.integers(1, 31))
        assert finish_after_work(intervals, start, seconds) == brute_finish(
            intervals, start, seconds
        )


@pytest.mark.parametrize("seed", SEEDS)
def test_next_working_equals_brute_force(seed: int) -> None:
    rng = np.random.default_rng(seed)
    intervals = random_intervals(rng)
    for t in range(HORIZON + 1):
        assert next_working(intervals, t) == brute_next(intervals, t)


@pytest.mark.parametrize("seed", SEEDS)
def test_intersect_equals_brute_force_membership(seed: int) -> None:
    rng = np.random.default_rng(seed)
    a, b = random_intervals(rng), random_intervals(rng)
    result = intersect(a, b)
    assert result is not None
    assert working_set(result) == working_set(a) & working_set(b)
    assert all(s < e for s, e in result)
    assert all(result[i][1] <= result[i + 1][0] for i in range(len(result) - 1))


# Edge cases on a fixed calendar: work [0, 10), break [10, 20), work [20, 30).
SHIFTS: list[Interval] = [(0.0, 10.0), (20.0, 30.0)]


def test_start_inside_break() -> None:
    assert next_working(SHIFTS, 15) == 20.0
    assert finish_after_work(SHIFTS, 15, 5) == 25.0
    assert work_seconds(SHIFTS, 15, 25) == 5.0
    assert not fits_within(SHIFTS, 15, 18)


def test_zero_length_work() -> None:
    assert finish_after_work(SHIFTS, 15, 0) == 15.0  # start is not moved
    assert finish_after_work(SHIFTS, 40, 0) == 40.0
    assert work_seconds(SHIFTS, 5, 5) == 0.0
    assert fits_within(SHIFTS, 5, 5)
    assert fits_within(SHIFTS, 0, 0)
    assert not fits_within(SHIFTS, 10, 10)  # an interval end is not a working instant
    assert not fits_within(SHIFTS, 15, 15)


def test_end_exactly_on_interval_end() -> None:
    assert fits_within(SHIFTS, 0, 10)
    assert fits_within(SHIFTS, 25, 30)
    assert not fits_within(SHIFTS, 5, 21)
    assert finish_after_work(SHIFTS, 5, 5) == 10.0  # earliest end, not the next shift start
    assert finish_after_work(SHIFTS, 0, 20) == 30.0


def test_no_further_working_time_returns_none() -> None:
    assert next_working(SHIFTS, 30) is None
    assert next_working(SHIFTS, 100) is None
    assert finish_after_work(SHIFTS, 25, 6) is None
    assert finish_after_work(SHIFTS, 0, 21) is None
    assert next_working([], 0) is None
    assert finish_after_work([], 0, 1) is None


def test_touching_intervals_form_one_working_run() -> None:
    touching: list[Interval] = [(0.0, 5.0), (5.0, 10.0)]
    assert fits_within(touching, 3, 7)
    assert finish_after_work(touching, 3, 4) == 7.0


def test_fractional_times() -> None:
    assert work_seconds(SHIFTS, 9.5, 20.25) == pytest.approx(0.75)
    assert finish_after_work(SHIFTS, 9.5, 1.0) == pytest.approx(20.5)
    assert next_working(SHIFTS, 10.0) == 20.0
    assert next_working(SHIFTS, 9.999) == 9.999


def test_none_means_always_working() -> None:
    always: Intervals = None
    assert fits_within(always, -5, 1e12)
    assert work_seconds(always, 3, 10) == 7.0
    assert finish_after_work(always, 3, 7) == 10.0
    assert next_working(always, -1) == -1
    assert intersect(None, None) is None
    assert intersect(None, SHIFTS) == SHIFTS
    assert intersect(SHIFTS, None) == SHIFTS


def test_reversed_ranges_are_rejected() -> None:
    with pytest.raises(ValueError):
        work_seconds(SHIFTS, 5, 4)
    with pytest.raises(ValueError):
        fits_within(SHIFTS, 5, 4)
    with pytest.raises(ValueError):
        finish_after_work(SHIFTS, 5, -1)


def test_large_calendar_uses_logarithmic_lookup() -> None:
    big = [(float(2 * k), float(2 * k + 1)) for k in range(1_000_000)]
    assert next_working(big, 1_999_997.5) == 1_999_998.0
    assert fits_within(big, 1_000_000.0, 1_000_001.0)
    assert work_seconds(big, 1_999_990, 1_999_996) == 3.0
