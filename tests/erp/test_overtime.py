"""ERP overtime: expansion, builder calendars and plan-side overtime seconds (T-219, COMP-126).

Requirements: REQ-182, REQ-183. Decision: D-125 (overtime is a priced subset of working
intervals). Calendar rules: COMP-114 (REQ-158).

Contract pinned here (the implementation follows these tests):

    from twinflow.erp.calendars import expand_overtime

    expand_overtime(overtime: twinflow.erp.schema.Overtime, tz: ZoneInfo,
                    as_of: datetime, horizon_end: datetime)
        -> tuple[tuple[float, float, float], ...]   # (start, end, cost_multiplier)

* The COMP-114 day walk: a local date uses its overtime exception's windows when one exists
  (empty = no overtime that date), else its weekday's windows; `end_minute <= start_minute`
  runs on into the next local date; wall-clock minutes localized at `fold=0` (spring-forward
  gap minutes skipped, the fall-back repeated hour counted once); seconds from `as_of`,
  clipped to `[0, horizon]`, sorted.
* Merge rule: touching or overlapping pieces with the SAME multiplier merge; touching pieces
  with DIFFERENT multipliers stay separate. (Generated inputs never overlap two different
  multipliers on one minute, so that case is not pinned.)

Builder (`build_problem_from_erp`), with `ErpPolicy.use_overtime` true (the default):

* A machine's overtime is `machine.overtime` when set, else its work center's (machine
  REPLACES work center; the two are never combined).
* A machine with overtime and a regular calendar `C` gets its own calendar
  `f"{C}+ot:{machine_id}"`: `intervals` = regular union overtime (merged, touching merged),
  `overtime` = overtime minus regular time, each remaining piece keeping its multiplier.
* A machine with overtime but NO calendar (machine and work center both None) is always
  working: it keeps `calendar_id = None`, and no `ot:{machine_id}` calendar is emitted.
* Machines without overtime keep the shared regular calendar id and intervals; inactive
  machines get no calendar of their own.
* `use_overtime = false`: no calendar has overtime, and the problem equals (except its id)
  the build of the same snapshot with every `overtime` field removed.

Plan side (`twinflow.application.planning.PlanningService`): every stored `plans` record
carries `overtime_seconds: dict[machine_id, float]` = the total seconds of that machine's
assignments `[start, end)` overlapping its calendar's `overtime` intervals. Machines with no
overlap may be absent or 0.0 (not pinned).
"""

from __future__ import annotations

import datetime as dt
import random
import sys
from collections.abc import Iterator, Sequence
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fixtures.generate import (
    OVERTIME_CENTER,
    OVERTIME_MACHINE,
    dst_variant,
    small_shop,
)

from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.erp.build import BuildResult, build_problem_from_erp
from twinflow.erp.calendars import expand, expand_overtime
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.profile import ErpPolicy, PlanningProfile
from twinflow.erp.schema import (
    Calendar,
    ErpSnapshot,
    Item,
    Lineage,
    Machine,
    OperationOption,
    OrderOperation,
    Overtime,
    OvertimeException,
    OvertimeWindow,
    ProductionOrder,
    SourceInfo,
    TimeWindow,
    WeekdayOvertime,
    WeekdayWindows,
    WorkCenter,
)
from twinflow.scheduling import (
    BaselineSolver,
    SchedulingProfile,
    SolveRequest,
    solve,
    validate,
)
from twinflow.scheduling import Calendar as ProblemCalendar

# The planning fake lives beside the planning tests (tests/ has no packages). Appended, not
# prepended, so it can never shadow a module of this directory.
_PLANNING_TESTS = Path(__file__).resolve().parents[1] / "planning"
if str(_PLANNING_TESTS) not in sys.path:
    sys.path.append(str(_PLANNING_TESTS))
from _fake_portfolio import FakePortfolioApi  # noqa: E402

CHICAGO = ZoneInfo("America/Chicago")
MINUTES_PER_DAY = 1440
SECONDS_PER_DAY = 86_400.0
SEEDS = range(30)
MULTIPLIERS = (1.0, 1.25, 1.5, 2.0)
SATURDAY = 5
SUNDAY = 6

Piece = tuple[float, float, float]  # (start, end, multiplier)
Interval = tuple[float, float]
Window = tuple[int, int, float]  # (start_minute, end_minute, multiplier)

FALL_BACK = (
    dt.datetime(2026, 10, 27, 5, 0, tzinfo=CHICAGO),
    dt.datetime(2026, 11, 6, 14, 30, tzinfo=CHICAGO),
)
SPRING_FORWARD = (
    dt.datetime(2027, 3, 9, 5, 0, tzinfo=CHICAGO),
    dt.datetime(2027, 3, 19, 14, 30, tzinfo=CHICAGO),
)


# --- overtime builders -----------------------------------------------------------------------


def _flat(rows: Any) -> list[float]:
    """pytest.approx rejects nested sequences: compare row lengths and values flat."""
    rows = list(rows)
    return [float(len(rows))] + [float(v) for row in rows for v in row]


def _windows(windows: Sequence[Window]) -> tuple[OvertimeWindow, ...]:
    return tuple(
        OvertimeWindow(start_minute=s, end_minute=e, cost_multiplier=m) for s, e, m in windows
    )


def make_overtime(
    week: dict[int, Sequence[Window]], exceptions: dict[dt.date, Sequence[Window]] | None = None
) -> Overtime:
    return Overtime(
        week=tuple(
            WeekdayOvertime(weekday=weekday, windows=_windows(windows))
            for weekday, windows in sorted(week.items())
        ),
        exceptions=tuple(
            OvertimeException(date=day, windows=_windows(windows))
            for day, windows in sorted((exceptions or {}).items())
        ),
    )


# --- minute oracle (independent of twinflow.erp.calendars) -----------------------------------


@cache
def _epoch_of_wall_minute(tz: ZoneInfo, day: dt.date, minute: int) -> float | None:
    """Epoch seconds of local wall-clock `minute` on `day` at fold=0; None inside a gap."""
    local = dt.datetime(day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=tz)
    epoch = local.timestamp()
    if dt.datetime.fromtimestamp(epoch, tz).replace(tzinfo=None) != local.replace(tzinfo=None):
        return None
    return epoch


def _overtime_windows_on(overtime: Overtime, day: dt.date) -> Sequence[OvertimeWindow]:
    for exception in overtime.exceptions:
        if exception.date == day:
            return exception.windows
    return [w for wd in overtime.week if wd.weekday == day.weekday() for w in wd.windows]


def overtime_oracle(
    overtime: Overtime, tz: ZoneInfo, as_of: dt.datetime, horizon_end: dt.datetime
) -> tuple[Piece, ...]:
    """Walk every overtime wall-clock minute; this is what `expand_overtime` must equal."""
    origin = as_of.timestamp()
    horizon = horizon_end.timestamp() - origin
    marked: dict[tuple[dt.date, int], float] = {}
    day = as_of.astimezone(tz).date() - dt.timedelta(days=1)
    last = horizon_end.astimezone(tz).date()
    while day <= last:
        for window in _overtime_windows_on(overtime, day):
            start, end, mult = window.start_minute, window.end_minute, window.cost_multiplier
            spans = [(day, start, end)] if end > start else [(day, start, MINUTES_PER_DAY)]
            if end <= start:
                spans.append((day + dt.timedelta(days=1), 0, end))
            for span_day, lo, hi in spans:
                for minute in range(lo, hi):
                    previous = marked.setdefault((span_day, minute), mult)
                    assert previous == mult, "generator must not overlap two multipliers"
        day += dt.timedelta(days=1)
    starts = sorted(
        (epoch - origin, mult)
        for (span_day, minute), mult in marked.items()
        if (epoch := _epoch_of_wall_minute(tz, span_day, minute)) is not None
    )
    merged: list[Piece] = []
    for start, mult in starts:
        lo, hi = max(start, 0.0), min(start + 60.0, horizon)
        if lo >= hi:
            continue
        if merged and lo <= merged[-1][1] and merged[-1][2] == mult:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi), mult)
        else:
            merged.append((lo, hi, mult))
    return tuple(merged)


def _pattern(rng: random.Random) -> list[tuple[int, int]]:
    """1-3 disjoint windows of a day; maybe a full day; the last maybe overnight.

    An overnight window ends at or before the first window's start, and every day uses the
    same pattern, so a carry-in never overlaps the next day's windows (it may touch).
    """
    if rng.random() < 0.1:
        return [(0, MINUTES_PER_DAY)]
    count = rng.randint(1, 3)
    points = sorted(rng.sample(range(0, MINUTES_PER_DAY + 1), 2 * count))
    windows = [(points[2 * i], points[2 * i + 1]) for i in range(count)]
    last_start = windows[-1][0]
    if rng.random() < 0.4 and last_start > 0:
        windows[-1] = (last_start, rng.randint(0, min(windows[0][0], last_start - 1)))
    return windows


def random_overtime(seed: int, as_of: dt.datetime, horizon_end: dt.datetime) -> Overtime:
    rng = random.Random(seed)  # noqa: S311 - seeded test data, not cryptography
    pattern = _pattern(rng)

    def priced() -> list[Window]:
        return [(s, e, rng.choice(MULTIPLIERS)) for s, e in pattern]

    week = {weekday: priced() for weekday in range(7) if rng.random() >= 0.25}
    first = as_of.astimezone(CHICAGO).date() - dt.timedelta(days=1)
    span = (horizon_end.astimezone(CHICAGO).date() - first).days
    days = rng.sample([first + dt.timedelta(days=d) for d in range(span + 1)], rng.randint(0, 4))
    exceptions = {day: ([] if rng.random() < 0.5 else priced()) for day in days}
    return make_overtime(week, exceptions)


def assert_well_formed(pieces: tuple[Piece, ...], horizon: float) -> None:
    previous: Piece | None = None
    for start, end, mult in pieces:
        assert 0.0 <= start < end <= horizon
        assert mult >= 1.0
        if previous is not None:
            assert start >= previous[1], "sorted and disjoint"
            if start == previous[1]:
                assert mult != previous[2], "touching equal multipliers must be merged"
        previous = (start, end, mult)


# --- expand_overtime: property against the oracle --------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize(
    ("as_of", "horizon_end"), [FALL_BACK, SPRING_FORWARD], ids=["fall-back", "spring-forward"]
)
def test_expand_overtime_matches_minute_oracle_across_dst(
    seed: int, as_of: dt.datetime, horizon_end: dt.datetime
) -> None:
    overtime = random_overtime(seed, as_of, horizon_end)

    result = expand_overtime(overtime, CHICAGO, as_of, horizon_end)

    assert isinstance(result, tuple)
    assert result == overtime_oracle(overtime, CHICAGO, as_of, horizon_end)
    assert_well_formed(result, horizon_end.timestamp() - as_of.timestamp())


# --- expand_overtime: hand-computed anchors --------------------------------------------------


MONDAY = dt.datetime(2026, 10, 5, 0, 0, tzinfo=CHICAGO)  # no DST change this week


def test_expand_overtime_saturday_window_carries_its_multiplier() -> None:
    overtime = make_overtime({SATURDAY: [(420, 900, 1.5)]})
    horizon_end = MONDAY + dt.timedelta(days=7)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    saturday = 5 * SECONDS_PER_DAY
    assert result == ((saturday + 25_200.0, saturday + 54_000.0, 1.5),)


def test_expand_overtime_touching_windows_with_different_multipliers_stay_separate() -> None:
    overtime = make_overtime({0: [(960, 1080, 1.5), (1080, 1200, 2.0)]})
    horizon_end = MONDAY + dt.timedelta(days=1)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    assert result == ((57_600.0, 64_800.0, 1.5), (64_800.0, 72_000.0, 2.0))


def test_expand_overtime_overlapping_windows_price_the_overlap_at_the_higher_multiplier() -> None:
    """Overlapping offers stay disjoint (TF-002 requires it); the overlap is never underpriced."""
    overtime = make_overtime({0: [(960, 1140, 1.5), (1080, 1200, 2.0), (1170, 1185, 1.25)]})
    horizon_end = MONDAY + dt.timedelta(days=1)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    assert result == ((57_600.0, 64_800.0, 1.5), (64_800.0, 72_000.0, 2.0))
    assert_well_formed(result, SECONDS_PER_DAY)


def test_expand_overtime_overnight_carry_in_overlapping_next_day_window_stays_disjoint() -> None:
    overtime = make_overtime({0: [(1320, 120, 1.5)], 1: [(60, 180, 2.0)]})
    horizon_end = MONDAY + dt.timedelta(days=2)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    tuesday = SECONDS_PER_DAY
    assert result == (
        (79_200.0, tuesday + 3_600.0, 1.5),
        (tuesday + 3_600.0, tuesday + 10_800.0, 2.0),
    )


def test_expand_overtime_touching_windows_with_equal_multipliers_merge() -> None:
    overtime = make_overtime({0: [(960, 1080, 1.5), (1080, 1200, 1.5)]})
    horizon_end = MONDAY + dt.timedelta(days=1)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    assert result == ((57_600.0, 72_000.0, 1.5),)


def test_expand_overtime_equal_multipliers_touching_across_midnight_merge() -> None:
    overtime = make_overtime({0: [(1200, MINUTES_PER_DAY, 1.5)], 1: [(0, 120, 1.5)]})
    horizon_end = MONDAY + dt.timedelta(days=2)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    assert result == ((72_000.0, 93_600.0, 1.5),)


def test_expand_overtime_overnight_window_runs_into_next_day() -> None:
    overtime = make_overtime({0: [(1320, 120, 1.5)]})  # Mon 22:00 .. Tue 02:00
    horizon_end = MONDAY + dt.timedelta(days=2)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    assert result == ((79_200.0, 93_600.0, 1.5),)


def test_expand_overtime_exception_replaces_that_weekdays_overtime() -> None:
    first_saturday = dt.date(2026, 10, 10)
    overtime = make_overtime({SATURDAY: [(420, 900, 1.5)]}, {first_saturday: [(480, 540, 2.0)]})
    horizon_end = MONDAY + dt.timedelta(days=14)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    first = 5 * SECONDS_PER_DAY
    second = 12 * SECONDS_PER_DAY
    assert result == (
        (first + 28_800.0, first + 32_400.0, 2.0),
        (second + 25_200.0, second + 54_000.0, 1.5),
    )


def test_expand_overtime_empty_exception_removes_that_days_overtime() -> None:
    overtime = make_overtime({SATURDAY: [(420, 900, 1.5)]}, {dt.date(2026, 10, 10): []})
    horizon_end = MONDAY + dt.timedelta(days=7)

    assert expand_overtime(overtime, CHICAGO, MONDAY, horizon_end) == ()


def test_expand_overtime_exception_on_a_weekday_without_overtime_adds_it() -> None:
    overtime = make_overtime({}, {dt.date(2026, 10, 7): [(1020, 1140, 1.25)]})  # Wednesday
    horizon_end = MONDAY + dt.timedelta(days=7)

    result = expand_overtime(overtime, CHICAGO, MONDAY, horizon_end)

    wednesday = 2 * SECONDS_PER_DAY
    assert result == ((wednesday + 61_200.0, wednesday + 68_400.0, 1.25),)


def test_expand_overtime_clips_to_as_of_and_horizon() -> None:
    overtime = make_overtime({0: [(420, 900, 1.5)]})
    as_of = MONDAY + dt.timedelta(hours=10)
    horizon_end = MONDAY + dt.timedelta(hours=12)

    result = expand_overtime(overtime, CHICAGO, as_of, horizon_end)

    assert result == ((0.0, 7_200.0, 1.5),)


def test_expand_overtime_empty_overtime_is_empty() -> None:
    assert expand_overtime(make_overtime({}), CHICAGO, *FALL_BACK) == ()


def test_expand_overtime_fall_back_night_counts_repeated_hour_once() -> None:
    # Sat 2026-10-31 22:00 .. Sun 06:00 overnight; Sunday 01:00-01:59 repeats (fall-back).
    overtime = make_overtime({SATURDAY: [(1320, 360, 1.5)]})
    as_of = dt.datetime(2026, 10, 31, 12, 0, tzinfo=CHICAGO)
    horizon_end = dt.datetime(2026, 11, 2, 0, 0, tzinfo=CHICAGO)

    result = expand_overtime(overtime, CHICAGO, as_of, horizon_end)

    assert result == ((36_000.0, 50_400.0, 1.5), (54_000.0, 68_400.0, 1.5))
    assert result == overtime_oracle(overtime, CHICAGO, as_of, horizon_end)


# --- builder helpers ---------------------------------------------------------------------------


def default_profile(**erp: Any) -> PlanningProfile:
    return PlanningProfile(scheduling=SchedulingProfile(), erp=ErpPolicy.model_validate(erp))


def redigest(snapshot: ErpSnapshot, **changes: Any) -> ErpSnapshot:
    edited = snapshot.model_copy(update={**changes, "digest": None})
    return edited.model_copy(update={"digest": snapshot_digest(edited)})


def edit_center(snapshot: ErpSnapshot, center_id: str, **changes: Any) -> ErpSnapshot:
    centers = tuple(
        wc.model_copy(update=changes) if wc.id == center_id else wc for wc in snapshot.work_centers
    )
    return redigest(snapshot, work_centers=centers)


def without_overtime(snapshot: ErpSnapshot) -> ErpSnapshot:
    return redigest(
        snapshot,
        work_centers=tuple(
            wc.model_copy(update={"overtime": None}) for wc in snapshot.work_centers
        ),
        machines=tuple(m.model_copy(update={"overtime": None}) for m in snapshot.machines),
    )


def ready(result: BuildResult) -> SolveRequest:
    assert result.status == "ready", [i.code for i in result.issues if i.severity == "hard"]
    assert result.request is not None
    return result.request


def horizon_end(snapshot: ErpSnapshot, profile: PlanningProfile) -> dt.datetime:
    """The builder's horizon: real elapsed days in UTC from `as_of` (build.py `_calendars`)."""
    return snapshot.as_of.astimezone(dt.UTC) + dt.timedelta(days=profile.erp.horizon_days)


def calendars_by_id(request: SolveRequest) -> dict[str, ProblemCalendar]:
    return {calendar.id: calendar for calendar in request.problem.calendars}


def machine_calendar_id(request: SolveRequest, machine_id: str) -> str | None:
    return next(m.calendar_id for m in request.problem.machines if m.id == machine_id)


def union(regular: Sequence[Interval], overtime: Sequence[Piece]) -> tuple[Interval, ...]:
    merged: list[Interval] = []
    for start, end in sorted([*regular, *((s, e) for s, e, _ in overtime)]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def minus(overtime: Sequence[Piece], regular: Sequence[Interval]) -> tuple[Piece, ...]:
    """Overtime pieces with regular time cut out; each remaining piece keeps its multiplier."""
    result: list[Piece] = []
    for start, end, mult in overtime:
        cursor = start
        for lo, hi in regular:
            if hi <= cursor or lo >= end:
                continue
            if lo > cursor:
                result.append((cursor, lo, mult))
            cursor = max(cursor, hi)
        if cursor < end:
            result.append((cursor, end, mult))
    return tuple(result)


def expected_machine_calendar(
    snapshot: ErpSnapshot, profile: PlanningProfile, regular_id: str, overtime: Overtime
) -> tuple[tuple[Interval, ...], tuple[Piece, ...]]:
    """(intervals, overtime pieces) the builder must emit, from the two oracles."""
    tz = ZoneInfo(snapshot.plant_timezone)
    end = horizon_end(snapshot, profile)
    source = next(c for c in snapshot.calendars if c.id == regular_id)
    regular = expand(source, tz, snapshot.as_of, end)
    pieces = overtime_oracle(overtime, tz, snapshot.as_of, end)
    return union(regular, pieces), minus(pieces, regular)


def as_pieces(calendar: ProblemCalendar) -> tuple[Piece, ...]:
    return tuple((o.start, o.end, o.cost_multiplier) for o in calendar.overtime)


def center(snapshot: ErpSnapshot, center_id: str) -> WorkCenter:
    return next(wc for wc in snapshot.work_centers if wc.id == center_id)


def machine(snapshot: ErpSnapshot, machine_id: str) -> Machine:
    return next(m for m in snapshot.machines if m.id == machine_id)


@pytest.fixture(scope="module")
def shop() -> ErpSnapshot:
    return small_shop()


@pytest.fixture(scope="module")
def shop_request(shop: ErpSnapshot) -> SolveRequest:
    return ready(build_problem_from_erp(shop, default_profile()))


# --- builder on small_shop -----------------------------------------------------------------------


def test_fixture_has_the_overtime_this_file_relies_on(shop: ErpSnapshot) -> None:
    form = center(shop, OVERTIME_CENTER)
    assert form.overtime is not None and form.calendar_id == "two_shift"
    override = machine(shop, OVERTIME_MACHINE)
    assert override.overtime is not None and override.work_center_id == OVERTIME_CENTER
    assert machine(shop, "form-1").overtime is None and machine(shop, "form-1").active
    assert machine(shop, "form-3").active is False


def test_build_machine_with_center_overtime_gets_its_own_calendar_id(
    shop_request: SolveRequest,
) -> None:
    assert machine_calendar_id(shop_request, "form-1") == "two_shift+ot:form-1"
    assert "two_shift+ot:form-1" in calendars_by_id(shop_request)


def test_build_machine_with_its_own_overtime_gets_its_own_calendar_id(
    shop_request: SolveRequest,
) -> None:
    assert machine_calendar_id(shop_request, OVERTIME_MACHINE) == "two_shift+ot:form-2"
    assert "two_shift+ot:form-2" in calendars_by_id(shop_request)


def test_build_machines_without_overtime_keep_the_shared_calendar(
    shop: ErpSnapshot, shop_request: SolveRequest
) -> None:
    calendars = calendars_by_id(shop_request)
    tz = ZoneInfo(shop.plant_timezone)
    end = horizon_end(shop, default_profile())
    for erp_machine in (m for m in shop.machines if m.active):
        if erp_machine.work_center_id == OVERTIME_CENTER:
            continue
        regular_id = center(shop, erp_machine.work_center_id).calendar_id
        assert regular_id is not None
        assert machine_calendar_id(shop_request, erp_machine.id) == regular_id, erp_machine.id
        source = next(c for c in shop.calendars if c.id == regular_id)
        assert calendars[regular_id].intervals == expand(source, tz, shop.as_of, end)
        assert calendars[regular_id].overtime == ()


def test_build_shared_regular_calendar_carries_no_overtime(shop_request: SolveRequest) -> None:
    # coil uses two_shift too; the shared two_shift calendar must stay overtime-free.
    assert calendars_by_id(shop_request)["two_shift"].overtime == ()


def test_build_inactive_machine_gets_no_overtime_calendar(shop_request: SolveRequest) -> None:
    assert not any(c.id.endswith("ot:form-3") for c in shop_request.problem.calendars)


def test_build_center_overtime_calendar_is_regular_union_overtime_at_1_5(
    shop: ErpSnapshot, shop_request: SolveRequest
) -> None:
    form = center(shop, OVERTIME_CENTER)
    assert form.overtime is not None
    intervals, pieces = expected_machine_calendar(
        shop, default_profile(), "two_shift", form.overtime
    )

    built = calendars_by_id(shop_request)["two_shift+ot:form-1"]

    assert _flat(built.intervals) == pytest.approx(_flat(intervals))
    assert _flat(as_pieces(built)) == pytest.approx(_flat(pieces))
    assert pieces, "the fixture's Saturday overtime must produce overtime pieces"
    assert {mult for _, _, mult in as_pieces(built)} == {1.5}


def test_build_machine_overtime_replaces_center_overtime_at_2_0(
    shop: ErpSnapshot, shop_request: SolveRequest
) -> None:
    override = machine(shop, OVERTIME_MACHINE).overtime
    assert override is not None
    intervals, pieces = expected_machine_calendar(shop, default_profile(), "two_shift", override)

    built = calendars_by_id(shop_request)["two_shift+ot:form-2"]

    assert _flat(built.intervals) == pytest.approx(_flat(intervals))
    assert _flat(as_pieces(built)) == pytest.approx(_flat(pieces))
    assert {mult for _, _, mult in as_pieces(built)} == {2.0}  # never the center's 1.5


def test_build_overtime_minus_regular_keeps_only_the_extra_time(shop: ErpSnapshot) -> None:
    # Monday 14:00-18:00 at 1.5 overlaps the 07:00-15:30 day shift: only 15:30-18:00 is
    # priced overtime, and the working interval becomes 07:00-18:00.
    overtime = make_overtime({0: [(840, 1080, 1.5)]})
    snapshot = edit_center(shop, OVERTIME_CENTER, overtime=overtime)
    profile = default_profile()
    intervals, pieces = expected_machine_calendar(snapshot, profile, "two_shift", overtime)

    built = calendars_by_id(ready(build_problem_from_erp(snapshot, profile)))["two_shift+ot:form-1"]

    assert _flat(built.intervals) == pytest.approx(_flat(intervals))
    assert _flat(as_pieces(built)) == pytest.approx(_flat(pieces))
    first_monday = as_pieces(built)[0]
    assert first_monday[1] - first_monday[0] == pytest.approx(2.5 * 3_600.0)


def test_build_overtime_entirely_inside_regular_time_prices_nothing(shop: ErpSnapshot) -> None:
    overtime = make_overtime({0: [(480, 600, 1.5)]})  # 08:00-10:00, inside the day shift
    snapshot = edit_center(shop, OVERTIME_CENTER, overtime=overtime)

    request = ready(build_problem_from_erp(snapshot, default_profile()))

    built = calendars_by_id(request)[machine_calendar_id(request, "form-1") or ""]
    assert built.overtime == ()
    assert built.intervals == calendars_by_id(request)["two_shift"].intervals


def test_build_overtime_exception_replaces_the_weekday_window(shop: ErpSnapshot) -> None:
    first_saturday = shop.as_of.astimezone(CHICAGO).date() + dt.timedelta(days=5)
    assert first_saturday.weekday() == SATURDAY
    overtime = make_overtime({SATURDAY: [(420, 900, 1.5)]}, {first_saturday: []})
    snapshot = edit_center(shop, OVERTIME_CENTER, overtime=overtime)
    profile = default_profile()
    _, pieces = expected_machine_calendar(snapshot, profile, "two_shift", overtime)

    built = calendars_by_id(ready(build_problem_from_erp(snapshot, profile)))["two_shift+ot:form-1"]

    assert _flat(as_pieces(built)) == pytest.approx(_flat(pieces))
    first_saturday_start = 5 * SECONDS_PER_DAY - 5 * 3_600.0  # as_of is Monday 05:00
    assert all(
        not (s < first_saturday_start + SECONDS_PER_DAY and e > first_saturday_start)
        for s, e, _ in as_pieces(built)
    )


def test_build_always_working_machine_gets_no_overtime_and_keeps_calendar_none(
    shop: ErpSnapshot,
) -> None:
    snapshot = edit_center(shop, OVERTIME_CENTER, calendar_id=None)

    request = ready(build_problem_from_erp(snapshot, default_profile()))

    assert machine_calendar_id(request, "form-1") is None
    assert machine_calendar_id(request, OVERTIME_MACHINE) is None
    ids = {c.id for c in request.problem.calendars}
    assert not {"ot:form-1", "ot:form-2"} & ids
    assert all(c.overtime == () for c in request.problem.calendars)


def test_build_overtime_calendars_are_valid_tf002_and_validate_clean(
    shop_request: SolveRequest,
) -> None:
    assert validate(shop_request) == []
    for calendar in shop_request.problem.calendars:
        ProblemCalendar.model_validate(calendar.model_dump())


def test_build_small_shop_with_overtime_solves_verified(shop_request: SolveRequest) -> None:
    result = solve(shop_request, solver=BaselineSolver())

    assert result.verified is True, result.issues


# --- use_overtime = false ------------------------------------------------------------------------


def _without_id(request: SolveRequest) -> dict[str, Any]:
    dumped = request.model_dump(mode="json")
    dumped["problem"].pop("id")
    return dumped


def test_build_use_overtime_false_emits_no_overtime(shop: ErpSnapshot) -> None:
    request = ready(build_problem_from_erp(shop, default_profile(use_overtime=False)))

    assert all(c.overtime == () for c in request.problem.calendars)
    assert not any("ot:" in c.id for c in request.problem.calendars)
    assert machine_calendar_id(request, "form-1") == "two_shift"
    assert machine_calendar_id(request, OVERTIME_MACHINE) == "two_shift"


def test_build_use_overtime_false_equals_build_without_overtime_fields(
    shop: ErpSnapshot,
) -> None:
    profile = default_profile(use_overtime=False)

    with_fields = ready(build_problem_from_erp(shop, profile))
    stripped = ready(build_problem_from_erp(without_overtime(shop), profile))

    assert _without_id(with_fields) == _without_id(stripped)


def test_build_use_overtime_defaults_true() -> None:
    assert ErpPolicy().use_overtime is True


# --- DST week ------------------------------------------------------------------------------------


def test_build_dst_week_overtime_matches_the_minute_oracle() -> None:
    # Saturday overnight 22:00-06:00 at 1.5 crosses the 2026-11-01 fall-back; Sunday
    # 06:00-09:00 at 1.25 touches its end with a different multiplier (stays separate).
    overtime = make_overtime({SATURDAY: [(1320, 360, 1.5)], SUNDAY: [(360, 540, 1.25)]})
    snapshot = edit_center(dst_variant(), OVERTIME_CENTER, overtime=overtime)
    profile = default_profile(horizon_days=14)
    intervals, pieces = expected_machine_calendar(snapshot, profile, "two_shift", overtime)

    built = calendars_by_id(ready(build_problem_from_erp(snapshot, profile)))["two_shift+ot:form-1"]

    assert _flat(built.intervals) == pytest.approx(_flat(intervals))
    assert _flat(as_pieces(built)) == pytest.approx(_flat(pieces))
    # The fall-back night (Sat 2026-10-31 22:00 .. Sun 06:00) is 8 working hours, not 9.
    fall_back_night = [p for p in pieces if p[2] == 1.5 and p[0] > 5 * SECONDS_PER_DAY][:2]
    assert sum(e - s for s, e, _ in fall_back_night) == pytest.approx(8 * 3_600.0)


# --- a plan that needs Saturday ------------------------------------------------------------------

TINY_AS_OF = dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.UTC)  # Monday
SATURDAY_OT = (5 * SECONDS_PER_DAY + 8 * 3_600.0, 5 * SECONDS_PER_DAY + 16 * 3_600.0)
JOBS_NEEDING_SATURDAY = 12  # two 30-minute ops fit each 1-hour weekday: 10 per week


def _lineage(table: str, *key: str) -> Lineage:
    return Lineage(table=table, key=key)


def _tiny_order(order_id: str, work_center: str, machine_id: str) -> ProductionOrder:
    option = OperationOption(
        work_center_id=work_center,
        machine_id=machine_id,
        setup_seconds=0.0,
        run_seconds_per_unit=60.0,
        preference=0,
        lineage=_lineage("options", order_id),
    )
    op = OrderOperation(
        op_id="10",
        sequence=10,
        work_center_id=work_center,
        options=(option,),
        setup_params={},
        queue_seconds=0.0,
        move_seconds=0.0,
        lineage=_lineage("order_ops", order_id, "10"),
        status="pending",
        qty_to_produce=30.0,
        qty_complete=0.0,
        qty_scrapped=0.0,
        qty_remaining=30.0,
    )
    return ProductionOrder(
        id=order_id,
        order_type="MO",
        item_id="WIDGET",
        quantity=30.0,
        status="released",
        rush=False,
        hold=False,
        due_date=dt.date(2026, 10, 6),
        sales_links=(),
        operations=(op,),
        materials=(),
        lineage=_lineage("orders", order_id),
    )


def saturday_shop() -> ErpSnapshot:
    """WC1 (machine M1) works 08:00-09:00 Mon-Fri plus Saturday overtime 08:00-16:00 at 1.5.

    Twelve 30-minute orders on M1 cannot all fit the five weekday hours, so the earliest
    plan uses Saturday. WC2 (machine M2) has the same regular calendar and no overtime.
    """
    hour = TimeWindow(start_minute=480, end_minute=540)
    calendar = Calendar(
        id="short",
        week=tuple(WeekdayWindows(weekday=d, windows=(hour,)) for d in range(5)),
        exceptions=(),
        lineage=_lineage("calendars", "short"),
    )
    orders = tuple(
        _tiny_order(f"MO-{n:02d}", "WC1", "M1") for n in range(1, JOBS_NEEDING_SATURDAY + 1)
    ) + (_tiny_order("MO-99", "WC2", "M2"),)
    snapshot = ErpSnapshot(
        schema_version="twinflow.erp/1",
        as_of=TINY_AS_OF,
        plant_timezone="UTC",
        source=SourceInfo(
            system="test", instance="sat", extractor="hand", extractor_version="1", generation=None
        ),
        source_issues=(),
        items=(Item(id="WIDGET", uom="ea", attributes={}, lineage=_lineage("items", "WIDGET")),),
        boms=(),
        routings=(),
        work_centers=(
            WorkCenter(
                id="WC1",
                calendar_id="short",
                attributes={},
                overtime=make_overtime({SATURDAY: [(480, 960, 1.5)]}),
                lineage=_lineage("wc", "WC1"),
            ),
            WorkCenter(id="WC2", calendar_id="short", attributes={}, lineage=_lineage("wc", "WC2")),
        ),
        machines=(
            Machine(id="M1", work_center_id="WC1", active=True, lineage=_lineage("m", "M1")),
            Machine(id="M2", work_center_id="WC2", active=True, lineage=_lineage("m", "M2")),
        ),
        calendars=(calendar,),
        labor_pools=(),
        production_orders=orders,
        sales_order_lines=(),
        customers=(),
        vendors=(),
        purchase_lines=(),
        inventory=(),
    )
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _overlap(start: float, end: float, window: Interval) -> float:
    return max(0.0, min(end, window[1]) - max(start, window[0]))


def test_baseline_plan_uses_saturday_overtime_and_verifies() -> None:
    request = ready(build_problem_from_erp(saturday_shop(), default_profile()))

    result = solve(request, solver=BaselineSolver())

    assert result.verified is True, result.issues
    on_m1 = [a for a in result.schedule.assignments if a.machine_id == "M1"]
    assert len(on_m1) == JOBS_NEEDING_SATURDAY
    assert sum(_overlap(a.start, a.end, SATURDAY_OT) for a in on_m1) > 0.0


def test_baseline_plan_without_overtime_never_works_saturday() -> None:
    request = ready(build_problem_from_erp(saturday_shop(), default_profile(use_overtime=False)))

    result = solve(request, solver=BaselineSolver())

    assert result.verified is True, result.issues
    assert all(_overlap(a.start, a.end, SATURDAY_OT) == 0.0 for a in result.schedule.assignments)


# --- plan records carry overtime_seconds per machine ---------------------------------------------


@pytest.fixture
def planning(tmp_path: Path) -> Iterator[tuple[PlanningService, Workspace]]:
    workspace = Workspace(tmp_path)
    service = PlanningService(workspace, portfolio=FakePortfolioApi())
    yield service, workspace
    service.close()
    workspace.close()


def expected_overtime_seconds(problem: dict[str, Any], plan: dict[str, Any]) -> dict[str, float]:
    """Independent recomputation from the stored problem request and plan assignments."""
    request = SolveRequest.model_validate(problem["request"])
    calendars = calendars_by_id(request)
    machine_calendar = {m.id: m.calendar_id for m in request.problem.machines}
    totals: dict[str, float] = {}
    for row in plan["assignments"]:
        calendar_id = machine_calendar[row["machine_id"]]
        if calendar_id is None:
            continue
        windows = [(o.start, o.end) for o in calendars[calendar_id].overtime]
        seconds = sum(_overlap(row["start"], row["end"], w) for w in windows)
        totals[row["machine_id"]] = totals.get(row["machine_id"], 0.0) + seconds
    return {machine_id: seconds for machine_id, seconds in totals.items() if seconds}


def _nonzero(values: dict[str, float]) -> dict[str, float]:
    return {key: value for key, value in values.items() if value}


def test_plan_records_carry_overtime_seconds_per_machine(
    planning: tuple[PlanningService, Workspace],
) -> None:
    service, workspace = planning
    built = service.build_problem(saturday_shop(), default_profile())
    assert built.status == "ready", built.issues
    portfolio = service.solve_portfolio(built.problem_id, request_key="ot-1", caller_id="arc")
    problem = workspace.repository.get("problems", built.problem_id)

    assert portfolio.plan_ids
    for plan_id in portfolio.plan_ids:
        plan = workspace.repository.get("plans", plan_id)
        assert "overtime_seconds" in plan, plan_id
        recorded = plan["overtime_seconds"]
        assert isinstance(recorded, dict)
        assert _nonzero(recorded) == pytest.approx(expected_overtime_seconds(problem, plan))
        assert recorded.get("M1", 0.0) > 0.0, "the Saturday work must show as overtime"
        assert recorded.get("M2", 0.0) == 0.0, "M2 has no overtime"


def test_plan_records_without_overtime_carry_zero_overtime_seconds(
    planning: tuple[PlanningService, Workspace],
) -> None:
    service, workspace = planning
    built = service.build_problem(saturday_shop(), default_profile(use_overtime=False))
    assert built.status == "ready", built.issues
    portfolio = service.solve_portfolio(built.problem_id, request_key="ot-0", caller_id="arc")

    for plan_id in portfolio.plan_ids:
        plan = workspace.repository.get("plans", plan_id)
        assert "overtime_seconds" in plan, plan_id
        assert _nonzero(plan["overtime_seconds"]) == {}
