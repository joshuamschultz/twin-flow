"""TF-002 `Calendar.overtime`: priced overtime as a subset of working time (T-219, COMP-126).

Requirements: REQ-182, REQ-183. Decision: D-125 (overtime is a priced subset of working
intervals; the verifier and every solver keep one working-time rule).

Contract pinned here (the implementation follows these tests):

    from twinflow.scheduling.model import Calendar, OvertimeInterval
    from twinflow.scheduling import OvertimeInterval  # facade re-export, same object

* `OvertimeInterval(start: float, end: float, cost_multiplier: float)`: frozen, unknown
  fields forbidden, finite floats only, `end > start`, `cost_multiplier >= 1`.
* `Calendar.overtime: tuple[OvertimeInterval, ...] = ()`. A calendar JSON without the key is
  still valid (schema_version stays "2.0").
* The validator rejects overtime entries that are unsorted, that overlap, or that do not lie
  inside ONE merged working interval. Touching `intervals` entries (`(0, 10), (10, 20)`)
  merge into one working interval `[0, 20)` for this check. Touching overtime entries are
  allowed (two windows with different multipliers stay separate).
* Overtime is working time: the verifier is unchanged, so a schedule that runs inside the
  overtime part of `intervals` verifies.

NOTE: `tests/scheduling/test_schema_file.py` (the `problem.schema.json` drift test) goes RED
when `Calendar.overtime` lands, until the implementer regenerates the checked-in schema.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from twinflow.scheduling import (
    SCHEMA_VERSION,
    BaselineSolver,
    Job,
    Machine,
    Operation,
    Problem,
    ProcessingOption,
    SolveRequest,
    WorkCenter,
    parse_request,
    request_json_schema,
    solve,
)
from twinflow.scheduling import OvertimeInterval as FacadeOvertimeInterval
from twinflow.scheduling.model import Calendar, OvertimeInterval


def _ot(start: float, end: float, multiplier: float = 1.5) -> OvertimeInterval:
    return OvertimeInterval(start=start, end=end, cost_multiplier=multiplier)


# --- OvertimeInterval ------------------------------------------------------------------------


def test_overtime_interval_holds_start_end_and_multiplier() -> None:
    interval = _ot(10.0, 20.0, 2.0)

    assert (interval.start, interval.end, interval.cost_multiplier) == (10.0, 20.0, 2.0)


def test_overtime_interval_multiplier_of_exactly_one_is_allowed() -> None:
    assert _ot(0.0, 1.0, 1.0).cost_multiplier == 1.0


@pytest.mark.parametrize("multiplier", [0.0, 0.5, 0.999999, -1.0])
def test_overtime_interval_multiplier_below_one_is_rejected(multiplier: float) -> None:
    with pytest.raises(ValidationError):
        _ot(0.0, 1.0, multiplier)


@pytest.mark.parametrize(("start", "end"), [(5.0, 5.0), (6.0, 5.0)])
def test_overtime_interval_end_not_after_start_is_rejected(start: float, end: float) -> None:
    with pytest.raises(ValidationError):
        _ot(start, end)


@pytest.mark.parametrize(
    "fields",
    [
        {"start": math.nan, "end": 1.0, "cost_multiplier": 1.5},
        {"start": 0.0, "end": math.inf, "cost_multiplier": 1.5},
        {"start": 0.0, "end": 1.0, "cost_multiplier": math.inf},
    ],
)
def test_overtime_interval_non_finite_numbers_are_rejected(fields: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        OvertimeInterval(**fields)


def test_overtime_interval_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        OvertimeInterval.model_validate(
            {"start": 0.0, "end": 1.0, "cost_multiplier": 1.5, "premium": 3.0}
        )


def test_overtime_interval_is_frozen() -> None:
    interval = _ot(0.0, 1.0)

    with pytest.raises(ValidationError):
        interval.start = 0.5  # type: ignore[misc]  # frozen model: assignment must fail


def test_overtime_interval_is_re_exported_from_the_scheduling_facade() -> None:
    assert FacadeOvertimeInterval is OvertimeInterval


# --- Calendar.overtime: defaults and old JSON ------------------------------------------------


def test_calendar_overtime_defaults_to_empty_tuple() -> None:
    calendar = Calendar(id="c", intervals=((0.0, 10.0),))

    assert calendar.overtime == ()


def test_calendar_json_without_overtime_key_is_still_valid() -> None:
    calendar = Calendar.model_validate({"id": "c", "intervals": [[0.0, 10.0]]})

    assert calendar.overtime == ()


def test_schema_version_stays_2_0() -> None:
    assert SCHEMA_VERSION == "2.0"


def test_calendar_overtime_round_trips_through_json() -> None:
    calendar = Calendar(
        id="c", intervals=((0.0, 100.0),), overtime=(_ot(10.0, 20.0, 1.5), _ot(20.0, 30.0, 2.0))
    )

    assert Calendar.model_validate_json(calendar.model_dump_json()) == calendar


def test_request_json_schema_describes_calendar_overtime() -> None:
    schema = request_json_schema()

    definitions = schema.get("$defs", {})
    assert "OvertimeInterval" in definitions
    assert "overtime" in definitions["Calendar"]["properties"]
    assert "overtime" not in definitions["Calendar"].get("required", [])


# --- Calendar.overtime: what the validator accepts -------------------------------------------


def test_calendar_accepts_overtime_inside_one_working_interval() -> None:
    calendar = Calendar(id="c", intervals=((0.0, 100.0),), overtime=(_ot(60.0, 100.0),))

    assert calendar.overtime == (_ot(60.0, 100.0),)


def test_calendar_accepts_overtime_equal_to_a_whole_working_interval() -> None:
    Calendar(id="c", intervals=((0.0, 50.0), (80.0, 100.0)), overtime=(_ot(80.0, 100.0),))


def test_calendar_accepts_touching_overtime_with_different_multipliers() -> None:
    overtime = (_ot(10.0, 20.0, 1.5), _ot(20.0, 30.0, 2.0))

    calendar = Calendar(id="c", intervals=((0.0, 100.0),), overtime=overtime)

    assert calendar.overtime == overtime


def test_calendar_accepts_overtime_spanning_touching_working_intervals() -> None:
    # (0, 10) and (10, 20) touch: one merged working interval [0, 20).
    Calendar(id="c", intervals=((0.0, 10.0), (10.0, 20.0)), overtime=(_ot(5.0, 15.0),))


def test_calendar_accepts_overtime_in_several_working_intervals() -> None:
    Calendar(
        id="c",
        intervals=((0.0, 10.0), (20.0, 30.0), (40.0, 50.0)),
        overtime=(_ot(0.0, 5.0), _ot(25.0, 30.0, 2.0), _ot(40.0, 50.0)),
    )


# --- Calendar.overtime: what the validator rejects -------------------------------------------


@pytest.mark.parametrize(
    ("intervals", "overtime"),
    [
        pytest.param(((0.0, 10.0),), ((5.0, 15.0),), id="runs-past-the-working-interval"),
        pytest.param(((10.0, 20.0),), ((5.0, 12.0),), id="starts-before-the-working-interval"),
        pytest.param(((0.0, 10.0),), ((20.0, 30.0),), id="entirely-outside"),
        pytest.param(((0.0, 10.0), (20.0, 30.0)), ((5.0, 25.0),), id="spans-a-gap"),
        pytest.param((), ((0.0, 1.0),), id="calendar-with-no-working-time"),
    ],
)
def test_calendar_rejects_overtime_not_inside_one_working_interval(
    intervals: tuple[tuple[float, float], ...], overtime: tuple[tuple[float, float], ...]
) -> None:
    with pytest.raises(ValidationError):
        Calendar(id="c", intervals=intervals, overtime=tuple(_ot(s, e) for s, e in overtime))


def test_calendar_rejects_unsorted_overtime() -> None:
    with pytest.raises(ValidationError):
        Calendar(id="c", intervals=((0.0, 100.0),), overtime=(_ot(50.0, 60.0), _ot(10.0, 20.0)))


@pytest.mark.parametrize(
    "overtime",
    [
        pytest.param(((10.0, 30.0), (20.0, 40.0)), id="partial-overlap"),
        pytest.param(((10.0, 40.0), (20.0, 30.0)), id="nested"),
        pytest.param(((10.0, 20.0), (10.0, 20.0)), id="duplicate"),
    ],
)
def test_calendar_rejects_overlapping_overtime(
    overtime: tuple[tuple[float, float], ...],
) -> None:
    with pytest.raises(ValidationError):
        Calendar(id="c", intervals=((0.0, 100.0),), overtime=tuple(_ot(s, e) for s, e in overtime))


# --- the request boundary and the verifier ---------------------------------------------------


def _request(calendar: dict[str, Any]) -> dict[str, Any]:
    """A one-op request on machine M1 whose calendar is `calendar` (as JSON)."""
    option = {"machine_id": "M1", "fixed_seconds": 50.0, "unit_seconds": 0.0, "preference_cost": 0}
    return {
        "schema_version": "2.0",
        "problem": {
            "id": "ot",
            "jobs": [
                {
                    "id": "J1",
                    "operations": [
                        {"id": "J1:10", "work_center_id": "WC", "quantity": 1, "options": [option]}
                    ],
                }
            ],
            "machines": [{"id": "M1", "work_center_id": "WC", "calendar_id": calendar["id"]}],
            "work_centers": [{"id": "WC"}],
            "calendars": [calendar],
        },
    }


def test_parse_request_accepts_a_calendar_with_overtime() -> None:
    calendar = {
        "id": "cal",
        "intervals": [[100.0, 200.0]],
        "overtime": [{"start": 100.0, "end": 200.0, "cost_multiplier": 1.5}],
    }

    parsed = parse_request(_request(calendar))

    assert isinstance(parsed, SolveRequest), parsed
    assert parsed.problem.calendars[0].overtime == (_ot(100.0, 200.0, 1.5),)


def test_parse_request_reports_overtime_outside_working_time_as_issues() -> None:
    calendar = {
        "id": "cal",
        "intervals": [[0.0, 50.0]],
        "overtime": [{"start": 100.0, "end": 200.0, "cost_multiplier": 1.5}],
    }

    parsed = parse_request(_request(calendar))

    assert not isinstance(parsed, SolveRequest)
    assert parsed, "an invalid overtime entry must produce at least one issue"


def test_schedule_inside_overtime_only_working_time_verifies() -> None:
    # The only working time is overtime: [100, 200) at 2.0. The op (50 s) must run there.
    calendar = Calendar(id="cal", intervals=((100.0, 200.0),), overtime=(_ot(100.0, 200.0, 2.0),))
    option = ProcessingOption(
        machine_id="M1", fixed_seconds=50.0, unit_seconds=0.0, preference_cost=0.0
    )
    problem = Problem(
        id="ot",
        jobs=(
            Job(
                id="J1",
                operations=(
                    Operation(id="J1:10", work_center_id="WC", quantity=1.0, options=(option,)),
                ),
            ),
        ),
        machines=(Machine(id="M1", work_center_id="WC", calendar_id="cal"),),
        work_centers=(WorkCenter(id="WC"),),
        calendars=(calendar,),
    )
    request = SolveRequest(schema_version="2.0", problem=problem)

    result = solve(request, solver=BaselineSolver())

    assert result.verified is True, result.issues
    (assignment,) = result.schedule.assignments
    assert 100.0 <= assignment.start and assignment.end <= 200.0
