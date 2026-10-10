"""Behavior tests for the problem validator and the boundary parse (T-096; COMP-006).

Requirements: REQ-071 (every issue, with code, JSON path and message), REQ-084 (bounded
boundary: schema errors become issues, never exceptions). Each issue code from COMP-006
has at least one failing problem. Checks the Pydantic model already enforces at parse
time are reached here through `model_construct` / `model_copy`, which skip validation,
exactly as a compiled problem can.

Path format pinned by these tests (matches `overrides.py`): dotted field names with
`[i]` list indices, rooted at the request, e.g.
`problem.jobs[0].operations[1].options[0].machine_id` and `overrides[2].id`. A compiled
problem uses the same paths, rooted at its `problem` field. The JSON root is `$`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

from twinflow.scheduling.model import (
    MAX_TEXT_LENGTH,
    BatchCapacity,
    Calendar,
    CompiledProblem,
    ExcludeMachineOverride,
    FrozenZone,
    HoldOverride,
    Job,
    LockOverride,
    Machine,
    Operation,
    OperationStatus,
    OperatorNeed,
    OperatorPool,
    Pin,
    Precedence,
    Problem,
    ProcessingOption,
    RestartRule,
    ScheduleIssue,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    WorkCenter,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.validator import parse_request, validate

FIXTURES = Path(__file__).parent / "fixtures"
FACT = {"fact_ref": "erp:order/42", "reason": "customer call"}
A1 = "problem.jobs[0].operations[0]"
A2 = "problem.jobs[0].operations[1]"
B1 = "problem.jobs[1].operations[0]"


# --- builders ------------------------------------------------------------------------------


def _option(machine_id: str, *, unit: float = 1.0) -> ProcessingOption:
    return ProcessingOption(
        machine_id=machine_id, fixed_seconds=10.0, unit_seconds=unit, preference_cost=0.0
    )


def _op(op_id: str, *machines: str, after: str | None = None, **fields: Any) -> Operation:
    fields.setdefault("work_center_id", "WC1")
    fields.setdefault("quantity", 10.0)
    fields.setdefault("options", tuple(_option(m) for m in machines or ("M1", "M2")))
    predecessors = (Precedence(operation_id=after, min_lag=5.0),) if after else ()
    fields.setdefault("predecessors", predecessors)
    return Operation(id=op_id, **fields)


def _problem(
    *,
    a1: Operation | None = None,
    a2: Operation | None = None,
    b1: Operation | None = None,
    **fields: Any,
) -> Problem:
    """Valid shop: J1 = A1 -> A2 on WC1 (M1, M2); J2 = B1 on WC2 (M3), with every resource."""
    j1 = Job(id="J1", operations=(a1 or _op("A1"), a2 or _op("A2", after="A1")))
    j2 = Job(id="J2", operations=(b1 or _op("B1", "M3", work_center_id="WC2"),))
    defaults: dict[str, Any] = {
        "id": "P",
        "jobs": (j1, j2),
        "machines": (
            Machine(id="M1", work_center_id="WC1", calendar_id="C1"),
            Machine(id="M2", work_center_id="WC1", setup_function_id="S1"),
            Machine(id="M3", work_center_id="WC2"),
        ),
        "work_centers": (WorkCenter(id="WC1", setup_function_id="S1"), WorkCenter(id="WC2")),
        "calendars": (Calendar(id="C1", intervals=((0.0, 100.0), (200.0, 300.0))),),
        "operator_pools": (OperatorPool(id="P1", size=2, calendar_id="C1"),),
        "setup_functions": (SetupFunction(id="S1", terms=()),),
    }
    return Problem(**{**defaults, **fields})


def _request(problem: Problem | None = None, **fields: Any) -> SolveRequest:
    return SolveRequest(schema_version="2.0", problem=problem or _problem(), **fields)


def _compiled(problem: Problem) -> CompiledProblem:
    """A compiled problem built without validation, as `model_copy` would leave it."""
    return apply_overrides(_request()).model_copy(update={"problem": problem})


def _codes(issues: list[ScheduleIssue]) -> list[str]:
    return [issue.code for issue in issues]


def _only(issues: list[ScheduleIssue], code: str) -> ScheduleIssue:
    assert _codes(issues) == [code], issues
    return issues[0]


def _with_machine(index: int, **update: Any) -> Problem:
    problem = _problem()
    machines = list(problem.machines)
    machines[index] = machines[index].model_copy(update=update)
    return problem.model_copy(update={"machines": tuple(machines)})


# --- clean inputs --------------------------------------------------------------------------


def test_validate_valid_request_and_its_compiled_problem_have_no_issues() -> None:
    request = _request()

    assert validate(request) == []
    assert validate(apply_overrides(request)) == []


@pytest.mark.parametrize("name", ["pinedo_7_1_1.json", "pinedo_14_2_1.json"])
def test_validate_pinedo_fixtures_are_clean(name: str) -> None:
    parsed = parse_request((FIXTURES / name).read_text())

    assert isinstance(parsed, SolveRequest)
    assert validate(parsed) == []
    assert validate(apply_overrides(parsed)) == []


# --- duplicate_id ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("update", "path"),
    [
        (
            {"jobs": (Job(id="J1", operations=(_op("A1"),)), Job(id="J1", operations=()))},
            "problem.jobs[1].id",
        ),
        (
            {
                "machines": (
                    Machine(id="M1", work_center_id="WC1"),
                    Machine(id="M2", work_center_id="WC1"),
                    Machine(id="M3", work_center_id="WC2"),
                    Machine(id="M2", work_center_id="WC1"),
                )
            },
            "problem.machines[3].id",
        ),
        (
            {"work_centers": (WorkCenter(id="WC1"), WorkCenter(id="WC2"), WorkCenter(id="WC1"))},
            "problem.work_centers[2].id",
        ),
        (
            {"calendars": (Calendar(id="C1", intervals=()), Calendar(id="C1", intervals=()))},
            "problem.calendars[1].id",
        ),
        (
            {"operator_pools": (OperatorPool(id="P1", size=1), OperatorPool(id="P1", size=1))},
            "problem.operator_pools[1].id",
        ),
        (
            {"setup_functions": (SetupFunction(id="S1", terms=()),) * 2},
            "problem.setup_functions[1].id",
        ),
    ],
)
def test_validate_duplicate_ids_per_collection(update: dict[str, Any], path: str) -> None:
    issue = _only(validate(_request(_problem(**update))), "duplicate_id")

    assert issue.path == path


def test_validate_duplicate_operation_id_across_jobs() -> None:
    problem = _problem(b1=_op("A1", "M3", work_center_id="WC2"))

    issue = _only(validate(_request(problem)), "duplicate_id")

    assert issue.path == f"{B1}.id"
    assert "A1" in issue.message


def test_validate_duplicate_override_ids() -> None:
    holds = tuple(
        HoldOverride(id="O1", hardness="hard", job_id="J1", until=5.0, **FACT) for _ in "ab"
    )

    issue = _only(validate(_request(overrides=holds)), "duplicate_id")

    assert issue.path == "overrides[1].id"


# --- unknown_reference ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("problem", "path"),
    [
        (_with_machine(0, work_center_id="WC9"), "problem.machines[0].work_center_id"),
        (_with_machine(0, calendar_id="C9"), "problem.machines[0].calendar_id"),
        (_with_machine(1, setup_function_id="S9"), "problem.machines[1].setup_function_id"),
        (
            _problem(
                work_centers=(WorkCenter(id="WC1", setup_function_id="S9"), WorkCenter(id="WC2"))
            ),
            "problem.work_centers[0].setup_function_id",
        ),
        (
            _problem(operator_pools=(OperatorPool(id="P1", size=1, calendar_id="C9"),)),
            "problem.operator_pools[0].calendar_id",
        ),
        (_problem(a2=_op("A2", after="A9")), f"{A2}.predecessors[0].operation_id"),
        (
            _problem(
                a1=_op("A1", operator_needs=(OperatorNeed(pool_id="P9", quantity=1, phase="run"),))
            ),
            f"{A1}.operator_needs[0].pool_id",
        ),
        (_problem(a1=_op("A1", pin=Pin(machine_id="M9"))), f"{A1}.pin.machine_id"),
    ],
)
def test_validate_unknown_reference(problem: Problem, path: str) -> None:
    issues = validate(_request(problem))

    assert "unknown_reference" in _codes(issues)
    assert path in [issue.path for issue in issues if issue.code == "unknown_reference"]


def test_validate_unknown_work_center_on_operation() -> None:
    problem = _problem(b1=_op("B1", "M3", work_center_id="WC9"))

    issues = validate(_request(problem))

    assert ("unknown_reference", f"{B1}.work_center_id") in [(i.code, i.path) for i in issues]


def test_validate_unknown_option_machine_path_is_fully_indexed() -> None:
    problem = _problem(a2=_op("A2", "M1", "M9", after="A1"))

    issue = _only(validate(_request(problem)), "unknown_reference")

    assert issue.path == "problem.jobs[0].operations[1].options[1].machine_id"
    assert "M9" in issue.message


def test_validate_unknown_status_machine() -> None:
    status = OperationStatus(state="in_progress", machine_id="M9", remaining_seconds=5.0)
    problem = _problem(a1=_op("A1", status=status))

    issues = validate(_request(problem))

    assert ("unknown_reference", f"{A1}.status.machine_id") in [(i.code, i.path) for i in issues]


# --- option_outside_work_center and no_options ----------------------------------------------


def test_validate_option_machine_outside_operation_work_center() -> None:
    problem = _problem(a1=_op("A1", "M1", "M3"))

    issue = _only(validate(_request(problem)), "option_outside_work_center")

    assert issue.path == f"{A1}.options[1].machine_id"


def test_validate_compiled_operation_without_options() -> None:
    empty = _op("A1").model_copy(update={"options": ()})

    issue = _only(validate(_compiled(_problem(a1=empty))), "no_options")

    assert issue.path == f"{A1}.options"


# --- cycle ----------------------------------------------------------------------------------


def test_validate_two_operation_cycle_reported_once_with_both_ids() -> None:
    problem = _problem(a1=_op("A1", after="A2"))

    issue = _only(validate(_request(problem)), "cycle")

    assert "A1" in issue.message and "A2" in issue.message
    assert issue.path == f"{A1}.predecessors"


def test_validate_self_loop_is_a_cycle() -> None:
    problem = _problem(b1=_op("B1", "M3", work_center_id="WC2", after="B1"))

    issue = _only(validate(_request(problem)), "cycle")

    assert issue.path == f"{B1}.predecessors"


def test_validate_two_disjoint_cycles_are_two_issues() -> None:
    problem = _problem(
        a1=_op("A1", after="A2"),
        b1=_op("B1", "M3", work_center_id="WC2", after="B1"),
    )

    assert _codes(validate(_request(problem))) == ["cycle", "cycle"]


# --- lag_bounds and overlap_requires_forbid -------------------------------------------------


def test_validate_compiled_max_lag_below_min_lag() -> None:
    link = Precedence.model_construct(
        operation_id="A1", min_lag=10.0, max_lag=5.0, overlap_quantity=None
    )
    problem = _problem(a2=_op("A2", after="A1").model_copy(update={"predecessors": (link,)}))

    issue = _only(validate(_compiled(problem)), "lag_bounds")

    assert issue.path == f"{A2}.predecessors[0]"


def test_validate_overlap_on_pause_operation_requires_forbid() -> None:
    link = Precedence(operation_id="A1", overlap_quantity=2.0)
    problem = _problem(a1=_op("A1", shift_crossing="pause"), a2=_op("A2", predecessors=(link,)))

    issue = _only(validate(_request(problem)), "overlap_requires_forbid")

    assert issue.path == f"{A2}.predecessors[0].overlap_quantity"


def test_validate_overlap_larger_than_predecessor_lot() -> None:
    link = Precedence(operation_id="A1", overlap_quantity=11.0)
    problem = _problem(a2=_op("A2", predecessors=(link,)))

    issue = _only(validate(_request(problem)), "overlap_requires_forbid")

    assert issue.path == f"{A2}.predecessors[0].overlap_quantity"


# --- batch_rules ----------------------------------------------------------------------------


def _batch_problem(op: Operation) -> Problem:
    return _with_machine(0, batch=BatchCapacity(capacity=20.0, unit="kg")).model_copy(
        update={"jobs": (Job(id="J1", operations=(op, _op("A2", after="A1"))), _problem().jobs[1])}
    )


def test_validate_batch_option_needs_zero_unit_seconds() -> None:
    op = _op("A1", batch_family="F", options=(_option("M1", unit=1.0),))

    issue = _only(validate(_request(_batch_problem(op))), "batch_rules")

    assert issue.path == f"{A1}.options[0].unit_seconds"


def test_validate_batch_quantity_above_capacity() -> None:
    op = _op("A1", batch_family="F", quantity=25.0, options=(_option("M1", unit=0.0),))

    issue = _only(validate(_request(_batch_problem(op))), "batch_rules")

    assert issue.path == f"{A1}.quantity"


def test_validate_batch_family_without_batch_machine() -> None:
    op = _op("A1", batch_family="F", options=(_option("M2", unit=0.0),))

    issue = _only(validate(_request(_batch_problem(op))), "batch_rules")

    assert issue.path == f"{A1}.batch_family"


def test_validate_valid_batch_operation_is_clean() -> None:
    op = _op("A1", batch_family="F", options=(_option("M1", unit=0.0),))

    assert validate(_request(_batch_problem(op))) == []


# --- restart_rule and calendar_order --------------------------------------------------------


@pytest.mark.parametrize(
    ("idle", "warmup"), [(0.0, 5.0), (5.0, -1.0), (math.inf, 5.0), (5.0, math.nan)]
)
def test_validate_compiled_restart_rule_bounds(idle: float, warmup: float) -> None:
    rule = RestartRule.model_construct(idle_threshold_seconds=idle, warmup_seconds=warmup)

    issue = _only(validate(_compiled(_with_machine(2, restart=rule))), "restart_rule")

    assert issue.path == "problem.machines[2].restart"


@pytest.mark.parametrize(
    "intervals",
    [((10.0, 5.0),), ((0.0, 10.0), (5.0, 20.0)), ((20.0, 30.0), (0.0, 10.0))],
)
def test_validate_compiled_calendar_order(intervals: tuple[tuple[float, float], ...]) -> None:
    calendar = Calendar.model_construct(id="C1", intervals=intervals)

    problem = _problem().model_copy(update={"calendars": (calendar,)})

    issue = _only(validate(_compiled(problem)), "calendar_order")

    assert issue.path.startswith("problem.calendars[0].intervals[")


# --- pins and in-progress -------------------------------------------------------------------


def test_validate_pin_machine_not_among_options() -> None:
    problem = _problem(a1=_op("A1", "M1", pin=Pin(machine_id="M2")))

    issue = _only(validate(_request(problem)), "pin_outside_options")

    assert issue.path == f"{A1}.pin.machine_id"


def test_validate_pin_window_earliest_after_latest() -> None:
    problem = _problem(a1=_op("A1", pin=Pin(earliest_start=50.0, latest_start=10.0)))

    issue = _only(validate(_request(problem)), "pin_window")

    assert issue.path == f"{A1}.pin"


@pytest.mark.parametrize(
    "status",
    [
        OperationStatus(state="in_progress", remaining_seconds=5.0),
        OperationStatus(state="in_progress", machine_id="M1"),
        OperationStatus(state="in_progress", machine_id="M3", remaining_seconds=5.0),
    ],
)
def test_validate_in_progress_needs_option_machine_and_remaining(status: OperationStatus) -> None:
    problem = _problem(a1=_op("A1", status=status))

    issue = _only(validate(_request(problem)), "in_progress_machine")

    assert issue.path == f"{A1}.status"


RUNNING_ON_M1 = OperationStatus(state="in_progress", machine_id="M1", remaining_seconds=4.0)


@pytest.mark.parametrize(
    ("pin", "path"),
    [
        (Pin(machine_id="M2"), f"{A1}.pin.machine_id"),
        (Pin(earliest_start=1.0), f"{A1}.pin"),
        (Pin(latest_start=-1.0), f"{A1}.pin"),
    ],
)
def test_validate_in_progress_op_whose_pin_excludes_its_status_is_in_progress_machine(
    pin: Pin, path: str
) -> None:
    problem = _problem(a1=_op("A1", status=RUNNING_ON_M1, pin=pin))

    issue = _only(validate(_request(problem)), "in_progress_machine")

    assert issue.path == path


def test_validate_in_progress_op_whose_pin_matches_its_status_is_valid() -> None:
    pin = Pin(machine_id="M1", earliest_start=0.0, latest_start=0.0)
    problem = _problem(a1=_op("A1", status=RUNNING_ON_M1, pin=pin))

    assert validate(_request(problem)) == []


def test_validate_two_in_progress_ops_on_one_machine_is_in_progress_machine() -> None:
    a1 = _op("A1", status=RUNNING_ON_M1)
    a2 = _op("A2", status=RUNNING_ON_M1, predecessors=())
    problem = _problem(a1=a1, a2=a2)

    issue = _only(validate(_request(problem)), "in_progress_machine")

    assert issue.path == f"{A2}.status.machine_id"
    assert "A1" in issue.message


def _held_predecessor_problem() -> Problem:
    """J1 (A1 -> A2) is held; B1 of the open job J2 waits on A1."""
    held = Job(id="J1", status="held", operations=(_op("A1"), _op("A2", after="A1")))
    successor = _op("B1", "M3", work_center_id="WC2", after="A1")
    return _problem(jobs=(held, Job(id="J2", operations=(successor,))))


def test_validate_scheduled_op_after_an_op_of_a_held_job_is_held_predecessor() -> None:
    issue = _only(validate(_request(_held_predecessor_problem())), "held_predecessor")

    assert issue.path == f"{B1}.predecessors[0]"


def test_validate_hold_override_on_a_predecessor_job_is_held_predecessor() -> None:
    problem = _problem(b1=_op("B1", "M3", work_center_id="WC2", after="A1"))
    hold = HoldOverride(id="h", hardness="hard", job_id="J1", **FACT)

    issue = _only(validate(_request(problem, overrides=(hold,))), "held_predecessor")

    assert issue.path == f"{B1}.predecessors[0]"


def test_validate_held_successor_of_a_held_job_is_valid() -> None:
    held = _held_predecessor_problem().jobs[0]
    successor = _op("B1", "M3", work_center_id="WC2", after="A1")
    problem = _problem(jobs=(held, Job(id="J2", status="held", operations=(successor,))))

    assert validate(_request(problem)) == []


# --- override issues ------------------------------------------------------------------------


def test_validate_request_reports_override_conflict() -> None:
    exclusions = tuple(
        ExcludeMachineOverride(id=f"X{m}", hardness="hard", operation_id="A1", machine_id=m, **FACT)
        for m in ("M1", "M2")
    )

    issue = _only(validate(_request(overrides=exclusions)), "override_conflict")

    assert issue.path == "overrides[1]"


def test_validate_request_reports_override_unknown_target() -> None:
    lock = LockOverride(id="L1", hardness="hard", operation_id="Z9", **FACT)

    issue = _only(validate(_request(overrides=(lock,))), "override_unknown_target")

    assert issue.path == "overrides[0]"


def test_validate_request_reports_frozen_zone_without_previous_plan() -> None:
    profile = SchedulingProfile(frozen_zone=FrozenZone(until=10.0, tolerance=0.0))

    issue = _only(validate(_request(profile=profile)), "frozen_requires_previous_plan")

    assert issue.path == "profile.frozen_zone"


def test_validate_compiled_returns_its_override_issues() -> None:
    lock = LockOverride(id="L1", hardness="hard", operation_id="Z9", **FACT)
    compiled = apply_overrides(_request(overrides=(lock,)))

    assert validate(compiled) == list(compiled.override_issues)


# --- schema_version and schema --------------------------------------------------------------


def test_validate_constructed_request_with_wrong_schema_version() -> None:
    request = _request().model_copy(update={"schema_version": "1.0"})

    issue = _only(validate(request), "schema_version")

    assert issue.path == "schema_version"


def _request_data() -> dict[str, Any]:
    data: dict[str, Any] = _request().model_dump(mode="json")
    return data


@pytest.mark.parametrize("version", ["1.0", None])
def test_parse_request_wrong_or_missing_schema_version(version: str | None) -> None:
    data = _request_data()
    if version is None:
        del data["schema_version"]
    else:
        data["schema_version"] = version

    result = parse_request(data)

    assert isinstance(result, list)
    issue = _only(result, "schema_version")
    assert issue.path == "schema_version"


def test_parse_request_valid_mapping_str_and_bytes() -> None:
    data = _request_data()
    text = json.dumps(data)

    for raw in (data, text, text.encode()):
        assert parse_request(raw) == _request()


def test_parse_request_reports_every_schema_error_with_indexed_paths() -> None:
    data = _request_data()
    data["problem"]["jobs"][0]["operations"][1]["options"][0]["unit_seconds"] = -1.0
    data["problem"]["machines"][2]["colour"] = "red"

    result = parse_request(data)

    assert isinstance(result, list)
    assert {(i.code, i.path) for i in result} == {
        ("schema", f"{A2}.options[0].unit_seconds"),
        ("schema", "problem.machines[2].colour"),
    }


def test_parse_request_union_tag_is_not_part_of_the_path() -> None:
    data = _request_data()
    data["overrides"] = [{"id": "L1", "kind": "lock", "hardness": "hard", **FACT}]

    result = parse_request(data)

    assert isinstance(result, list)
    issue = _only(result, "schema")
    assert issue.path == "overrides[0].operation_id"


def test_parse_request_invalid_json_is_a_root_schema_issue() -> None:
    result = parse_request("{not json")

    assert isinstance(result, list)
    assert _only(result, "schema").path == "$"


def test_parse_request_bounds_attacker_sized_paths() -> None:
    data = _request_data()
    data["problem"]["x" * (MAX_TEXT_LENGTH * 3)] = 1

    result = parse_request(data)

    assert isinstance(result, list)
    assert len(_only(result, "schema").path) <= MAX_TEXT_LENGTH


# --- every issue, not only the first --------------------------------------------------------


def test_validate_reports_five_different_defects_together() -> None:
    status = OperationStatus(state="in_progress", remaining_seconds=5.0)
    problem = _problem(
        a1=_op("A1", after="A2", pin=Pin(earliest_start=9.0, latest_start=1.0)),
        a2=_op("A2", "M1", "M9", after="A1"),
        b1=_op("B1", "M3", work_center_id="WC2", status=status),
        machines=(
            Machine(id="M1", work_center_id="WC1"),
            Machine(id="M2", work_center_id="WC1"),
            Machine(id="M3", work_center_id="WC2"),
            Machine(id="M3", work_center_id="WC2"),
        ),
    )

    issues = validate(_request(problem))

    assert sorted(_codes(issues)) == [
        "cycle",
        "duplicate_id",
        "in_progress_machine",
        "pin_window",
        "unknown_reference",
    ]
    assert all(issue.path.startswith("problem.") and issue.message for issue in issues)


def test_validate_duplicate_ids_skip_override_compile_instead_of_crashing() -> None:
    twin = Job(id="J1", operations=(_op("A1"), _op("A1")))
    hold = HoldOverride(id="H1", hardness="hard", job_id="J1", **FACT)

    issues = validate(_request(_problem(jobs=(twin,)), overrides=(hold,)))

    assert _codes(issues) == ["duplicate_id"]
