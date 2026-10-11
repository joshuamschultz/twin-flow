"""The horizon is a planning window, not a wall (TF-005 review ruling A, builder side).

Contract pinned here (the implementation follows these tests):

* PINNED: `twinflow.erp.calendars.expansion_days(policy: ErpPolicy) -> int` =
  `min(4 * policy.horizon_days, 730)` when `hard_horizon` is false.
* `ErpPolicy.hard_horizon: StrictBool = False`, set with `[erp] hard_horizon = true`.
* Default (`hard_horizon` false): every calendar repeats its weekly pattern and exceptions
  past the horizon up to the expansion cap end = `as_of` + `expansion_days` REAL elapsed
  days, i.e. the built intervals equal `expand(calendar, tz, as_of, cap_end)`;
  `Problem.horizon_end` stays None.
* `hard_horizon = true`: calendars stop at the horizon end (`expand(..., horizon_end)`) and
  `Problem.horizon_end = horizon_seconds(horizon_days)`.
* DEV-11 hold (`release_after_horizon`): only when the release (or an op's material
  readiness) is at or after the cap end, or the horizon end when `hard_horizon` is true. A
  job released between the horizon end and the cap end is planned normally.
* PINNED: `BuildResult.horizon_seconds: float` = `horizon_seconds(horizon_days)` (the
  planning window length), in both modes.
* An overloaded shop (more work than the window holds) solves with the baseline to a
  verified, late plan, not UNKNOWN.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _factories import lineage

from twinflow.erp.build import BuildResult, build_problem_from_erp
from twinflow.erp.calendars import expand, horizon_seconds
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.profile import ErpPolicy, PlanningProfile, ProfileError, load_planning_profile
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling import BaselineSolver, SolveRequest, solve

AS_OF = "2026-10-12T05:00:00-05:00"  # Monday 05:00 CDT
TZ = ZoneInfo("America/Chicago")
DAY = 86_400.0
# Plant closed on Thursday 2026-10-22: day 10, past a 7-day horizon, inside its 28-day cap.
CLOSED_DAY = "2026-10-22"

Data = dict[str, Any]


# --- snapshot builders --------------------------------------------------------------------------


def _day_calendar() -> Data:
    window = {"start_minute": 420, "end_minute": 930}  # 07:00-15:30
    return {
        "id": "day",
        "week": [{"weekday": d, "windows": [window]} for d in range(5)],
        "exceptions": [{"date": CLOSED_DAY, "windows": [], "description": "holiday"}],
        "lineage": lineage("calendar", "day"),
    }


def _order(order_id: str, run_seconds: float, **fields: Any) -> Data:
    op = {
        "op_id": "10",
        "sequence": 10,
        "work_center_id": "form",
        "options": [
            {
                "work_center_id": "form",
                "machine_id": "form-1",
                "setup_seconds": 0.0,
                "run_seconds_per_unit": run_seconds / 10.0,
                "fixed_run_seconds": 0.0,
                "preference": 0,
                "lineage": lineage("option", order_id),
            }
        ],
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "status": "pending",
        "qty_to_produce": 10.0,
        "qty_complete": 0.0,
        "qty_scrapped": 0.0,
        "qty_remaining": 10.0,
        "lineage": lineage("order_operation", order_id, "10"),
    }
    return {
        "id": order_id,
        "order_type": "MO",
        "item_id": "SPR",
        "quantity": 10.0,
        "status": "released",
        "rush": False,
        "hold": False,
        "due_date": "2026-10-14",
        "sales_links": [],
        "operations": [op],
        "materials": [],
        "lineage": lineage("production_order", order_id),
    } | fields


def _snapshot(orders: list[Data]) -> ErpSnapshot:
    data: Data = {
        "schema_version": "twinflow.erp/1",
        "as_of": AS_OF,
        "plant_timezone": "America/Chicago",
        "source": {
            "system": "hand",
            "instance": "horizon",
            "extractor": "test",
            "extractor_version": "1",
            "generation": None,
        },
        "source_issues": [],
        "items": [{"id": "SPR", "uom": "ea", "attributes": {}, "lineage": lineage("item", "SPR")}],
        "boms": [],
        "routings": [],
        "work_centers": [
            {
                "id": "form",
                "calendar_id": "day",
                "attributes": {},
                "lineage": lineage("wc", "form"),
            }
        ],
        "machines": [
            {
                "id": "form-1",
                "work_center_id": "form",
                "efficiency": 1.0,
                "active": True,
                "lineage": lineage("machine", "form-1"),
            }
        ],
        "calendars": [_day_calendar()],
        "labor_pools": [],
        "production_orders": orders,
        "sales_order_lines": [],
        "customers": [],
        "vendors": [],
        "purchase_lines": [],
        "inventory": [],
    }
    snapshot = ErpSnapshot.model_validate(data)
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _profile(erp_toml: str = "") -> PlanningProfile:
    return load_planning_profile(f"[erp]\n{erp_toml}")


def _ready(result: BuildResult) -> SolveRequest:
    hard = sorted({(i.code, i.path) for i in result.issues if i.severity == "hard"})
    assert result.status == "ready", hard
    assert result.request is not None
    return result.request


def _days_after_as_of(snapshot: ErpSnapshot, days: int) -> dt.datetime:
    """`days` REAL elapsed days after `as_of` (the cap is DST-safe, like the horizon)."""
    return snapshot.as_of.astimezone(dt.UTC) + dt.timedelta(days=days)


def _intervals(request: SolveRequest) -> tuple[tuple[float, float], ...]:
    (calendar,) = request.problem.calendars
    return calendar.intervals


def _codes(result: BuildResult) -> list[str]:
    return [issue.code for issue in result.issues]


SMALL_ORDER = [_order("MO-1", 3_600.0)]


# --- the expansion cap --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("horizon_days", "expected"),
    [(1, 4), (7, 28), (56, 224), (182, 728), (183, 730), (200, 730), (366, 730)],
)
def test_expansion_days_is_four_horizons_capped_at_730(horizon_days: int, expected: int) -> None:
    from twinflow.erp.calendars import expansion_days

    assert expansion_days(ErpPolicy(horizon_days=horizon_days)) == expected


def test_erp_policy_hard_horizon_defaults_false_and_loads_from_toml() -> None:
    assert ErpPolicy().hard_horizon is False
    assert _profile("hard_horizon = true\n").erp.hard_horizon is True


def test_profile_rejects_a_non_boolean_hard_horizon_with_its_key_path() -> None:
    with pytest.raises(ProfileError, match=r"erp\.hard_horizon"):
        _profile('hard_horizon = "yes"\n')


# --- calendars past the horizon -----------------------------------------------------------------


def test_build_calendar_runs_past_the_horizon_to_the_expansion_cap() -> None:
    snapshot = _snapshot(SMALL_ORDER)
    request = _ready(build_problem_from_erp(snapshot, _profile("horizon_days = 7\n")))
    source = snapshot.calendars[0]
    cap_end = _days_after_as_of(snapshot, 28)
    assert _intervals(request) == expand(source, TZ, snapshot.as_of, cap_end)
    assert _intervals(request)[-1][1] > 21 * DAY  # the fourth week is working time too


def test_build_calendar_keeps_exceptions_past_the_horizon() -> None:
    snapshot = _snapshot(SMALL_ORDER)
    request = _ready(build_problem_from_erp(snapshot, _profile("horizon_days = 7\n")))
    closed = dt.date.fromisoformat(CLOSED_DAY)
    start = dt.datetime.combine(closed, dt.time(), tzinfo=TZ).timestamp()
    end = dt.datetime.combine(closed + dt.timedelta(days=1), dt.time(), tzinfo=TZ).timestamp()
    lo, hi = start - snapshot.as_of.timestamp(), end - snapshot.as_of.timestamp()
    assert lo > 7 * DAY
    assert [(s, e) for s, e in _intervals(request) if s < hi and e > lo] == []
    # The same weekday one week earlier (inside the horizon) and later both work.
    assert any(s < lo - 6 * DAY and e > lo - 7 * DAY for s, e in _intervals(request))
    assert any(s < hi + 7 * DAY and e > lo + 7 * DAY for s, e in _intervals(request))


def test_build_problem_horizon_end_stays_none_by_default() -> None:
    request = _ready(build_problem_from_erp(_snapshot(SMALL_ORDER), _profile("horizon_days = 7\n")))
    assert request.problem.horizon_end is None


@pytest.mark.parametrize(
    "erp_toml", ["horizon_days = 7\n", "horizon_days = 7\nhard_horizon = true\n"]
)
def test_build_result_exposes_the_planning_window_length(erp_toml: str) -> None:
    result = build_problem_from_erp(_snapshot(SMALL_ORDER), _profile(erp_toml))
    _ready(result)
    assert result.horizon_seconds == horizon_seconds(7) == 7 * DAY


def test_build_hard_horizon_stops_calendars_at_the_horizon_and_sets_horizon_end() -> None:
    snapshot = _snapshot(SMALL_ORDER)
    profile = _profile("horizon_days = 7\nhard_horizon = true\n")
    request = _ready(build_problem_from_erp(snapshot, profile))
    horizon_end = _days_after_as_of(snapshot, 7)
    assert _intervals(request) == expand(snapshot.calendars[0], TZ, snapshot.as_of, horizon_end)
    assert request.problem.horizon_end == horizon_seconds(7)


def test_build_hard_horizon_is_part_of_the_problem_identity() -> None:
    snapshot = _snapshot(SMALL_ORDER)
    soft = build_problem_from_erp(snapshot, _profile("horizon_days = 7\n"))
    hard = build_problem_from_erp(snapshot, _profile("horizon_days = 7\nhard_horizon = true\n"))
    assert soft.problem_digest != hard.problem_digest
    assert _ready(soft).problem.id != _ready(hard).problem.id


# --- the hold rule ------------------------------------------------------------------------------


def test_build_release_between_horizon_end_and_cap_is_planned_normally() -> None:
    # Horizon 7 days ends 2026-10-19 05:00 CDT; the cap ends 2026-11-09. Released 10-20.
    snapshot = _snapshot([_order("MO-1", 3_600.0, release_date="2026-10-20")])
    result = build_problem_from_erp(snapshot, _profile("horizon_days = 7\n"))
    request = _ready(result)
    (job,) = request.problem.jobs
    assert job.status == "open"
    assert job.release > horizon_seconds(7)
    assert "release_after_horizon" not in _codes(result)
    solved = solve(request, solver=BaselineSolver())
    assert solved.verified is True, solved.issues
    (assignment,) = solved.schedule.assignments
    assert assignment.start >= job.release


def test_build_hard_horizon_holds_a_release_after_the_horizon_end() -> None:
    snapshot = _snapshot([_order("MO-1", 3_600.0, release_date="2026-10-20")])
    result = build_problem_from_erp(snapshot, _profile("horizon_days = 7\nhard_horizon = true\n"))
    (job,) = _ready(result).problem.jobs
    assert job.status == "held"
    assert _codes(result).count("release_after_horizon") == 1


def test_build_release_on_the_cap_day_is_held() -> None:
    # The cap end is 2026-11-09 04:00 CST (28 real days, across fall-back); a release date of
    # 2026-11-10 starts after it, 2026-11-08 before it.
    late = _order("MO-1", 3_600.0, release_date="2026-11-10")
    inside = _order("MO-2", 3_600.0, release_date="2026-11-08")
    result = build_problem_from_erp(_snapshot([late, inside]), _profile("horizon_days = 7\n"))
    jobs = {job.id: job.status for job in _ready(result).problem.jobs}
    assert jobs == {"MO-1": "held", "MO-2": "open"}
    held = [i for i in result.issues if i.code == "release_after_horizon"]
    assert [i.lineage.key for i in held if i.lineage is not None] == [("MO-1",)]


# --- overload past the window -------------------------------------------------------------------


def test_build_overloaded_window_solves_to_a_verified_late_plan() -> None:
    # Window: Mon 07:00-15:30 + Tue 07:00-15:30 = 17 h before the 2-day horizon ends (Wed
    # 05:00). Four 6 h orders need 24 h, so the last runs past the window, inside the 8-day cap.
    orders = [_order(f"MO-{n}", 6 * 3_600.0) for n in range(1, 5)]
    request = _ready(build_problem_from_erp(_snapshot(orders), _profile("horizon_days = 2\n")))
    solved = solve(request, solver=BaselineSolver())
    assert solved.schedule.status in {"FEASIBLE", "OPTIMAL"}, solved.issues
    assert solved.verified is True, solved.issues
    ends = [assignment.end for assignment in solved.schedule.assignments]
    assert len(ends) == 4
    assert max(ends) > horizon_seconds(2)
    assert max(ends) <= 8 * DAY


def test_build_overloaded_window_with_hard_horizon_is_not_silently_late() -> None:
    # With the wall back on, the same overload must not come back as a verified plan that
    # runs past the horizon.
    orders = [_order(f"MO-{n}", 6 * 3_600.0) for n in range(1, 5)]
    profile = _profile("horizon_days = 2\nhard_horizon = true\n")
    request = _ready(build_problem_from_erp(_snapshot(orders), profile))
    solved = solve(request, solver=BaselineSolver())
    assert all(a.end <= horizon_seconds(2) for a in solved.schedule.assignments)
    assert not (solved.verified and len(solved.schedule.assignments) == 4)
