"""Contract tests for the problem model v2 types (T-085, SDD COMP-001 and Data Model).

These tests pin the public names, field names, discriminators and bounds of
`twinflow.scheduling.model`. They assert observable validation behavior only: what parses,
what is rejected (and at which field), JSON round-trips, immutability and the schema hook.
Requirements: REQ-061, REQ-062, REQ-063, REQ-070, REQ-084. Decisions: D-060 to D-068.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from twinflow.scheduling import model as model_module
from twinflow.scheduling.model import (
    SCHEMA_VERSION,
    AbsDiffTerm,
    Assignment,
    BatchCapacity,
    Calendar,
    CompiledProblem,
    EpsilonBound,
    EpsilonConstraint,
    ExcludeMachineOverride,
    ExpediteOverride,
    FamilyMatrixEntry,
    FrozenZone,
    HoldOverride,
    Job,
    Lexicographic,
    LexicographicLevel,
    LockOverride,
    Machine,
    MachineState,
    MismatchTerm,
    ObjectiveKind,
    ObjectiveSpec,
    Operation,
    OperationStatus,
    OperatorNeed,
    OperatorPool,
    Override,
    Pin,
    Precedence,
    PreferMachineOverride,
    PreviousAssignment,
    PreviousPlan,
    PriorityOverride,
    Problem,
    ProcessingOption,
    RestartRule,
    Schedule,
    ScheduleIssue,
    SchedulingProfile,
    SetupFunction,
    SetupTerm,
    SolveRequest,
    SolveResult,
    SplitOverride,
    VerificationResult,
    WeightedSum,
    WeightedTerm,
    WorkCenter,
    request_json_schema,
)

Data = dict[str, Any]
Target = type[BaseModel] | TypeAdapter[Any]

OVERRIDE = TypeAdapter(Override)
OBJECTIVE_SPEC = TypeAdapter(ObjectiveSpec)
SETUP_TERM = TypeAdapter(SetupTerm)

NON_FINITE = [math.nan, math.inf, -math.inf]
ID_LIMIT = 200
TEXT_LIMIT = 2000


# --- minimal valid payloads ---------------------------------------------------------------


def option() -> Data:
    return {"machine_id": "m1", "fixed_seconds": 0.0, "unit_seconds": 1.0, "preference_cost": 0.0}


def operation(op_id: str = "o1") -> Data:
    return {"id": op_id, "work_center_id": "wc1", "quantity": 1.0, "options": [option()]}


def job() -> Data:
    return {"id": "j1", "operations": [operation()]}


def machine() -> Data:
    return {"id": "m1", "work_center_id": "wc1"}


def work_center() -> Data:
    return {"id": "wc1"}


def problem() -> Data:
    return {"id": "p1", "jobs": [job()], "machines": [machine()], "work_centers": [work_center()]}


def lexicographic() -> Data:
    return {"mode": "lexicographic", "levels": [{"kind": "makespan"}]}


def weighted_sum() -> Data:
    return {"mode": "weighted_sum", "terms": [{"kind": "makespan", "weight": 1.0}]}


def epsilon_constraint() -> Data:
    return {
        "mode": "epsilon_constraint",
        "primary": "makespan",
        "bounds": [{"kind": "total_setup", "upper": 100.0}],
    }


def profile() -> Data:
    return {
        "id": "default",
        "class_weights": {1: 1.0, 2: 2.0, 4: 4.0},
        "objective": lexicographic(),
        "time_budget_seconds": 10.0,
        "robustness_load_targets": [0.9, 0.8, 0.7],
        "default_portfolio": ["baseline"],
    }


def request() -> Data:
    return {"schema_version": "2.0", "problem": problem()}


def assignment() -> Data:
    return {"operation_id": "o1", "machine_id": "m1", "start": 0.0, "setup_end": 1.0, "end": 2.0}


def issue() -> Data:
    return {"code": "schema", "path": "$.problem", "message": "bad"}


def schedule() -> Data:
    return {
        "status": "FEASIBLE",
        "assignments": [assignment()],
        "solver": "baseline",
        "elapsed_seconds": 0.1,
    }


def previous_assignment() -> Data:
    return {"operation_id": "o1", "machine_id": "m1", "start": 0.0}


def previous_plan() -> Data:
    return {"plan_id": "plan1", "problem_digest": "abc", "assignments": [previous_assignment()]}


def calendar() -> Data:
    return {"id": "c1", "intervals": [[0.0, 10.0], [20.0, 30.0]]}


def abs_diff() -> Data:
    return {"kind": "abs_diff", "param": "width", "seconds_per_unit": 2.0}


def mismatch() -> Data:
    return {"kind": "mismatch", "param": "color", "seconds": 30.0}


def setup_function() -> Data:
    return {
        "id": "sf1",
        "terms": [abs_diff(), mismatch()],
        "family_matrix": [{"from_family": "a", "to_family": "b", "seconds": 5.0}],
    }


def _override(kind: str, **targets: Any) -> Data:
    return {
        "kind": kind,
        "id": f"ov-{kind}",
        "hardness": "hard",
        "fact_ref": "fact:1",
        "reason": "customer call",
        **targets,
    }


def lock() -> Data:
    return _override("lock", operation_id="o1", machine_id="m1", start=5.0)


def expedite() -> Data:
    return _override("expedite", job_id="j1", due=50.0)


def priority() -> Data:
    return _override("priority", job_id="j1", weight_class=4)


def exclude_machine() -> Data:
    return _override("exclude_machine", operation_id="o1", machine_id="m1")


def prefer_machine() -> Data:
    return _override("prefer_machine", job_id="j1", machine_id="m1")


def split() -> Data:
    return _override("split", job_id="j1", fractions=[0.5, 0.5])


def hold() -> Data:
    return _override("hold", job_id="j1", until=100.0)


OVERRIDE_FACTORIES: dict[str, Callable[[], Data]] = {
    "lock": lock,
    "expedite": expedite,
    "priority": priority,
    "exclude_machine": exclude_machine,
    "prefer_machine": prefer_machine,
    "split": split,
    "hold": hold,
}

OVERRIDE_CLASSES: dict[str, type[BaseModel]] = {
    "lock": LockOverride,
    "expedite": ExpediteOverride,
    "priority": PriorityOverride,
    "exclude_machine": ExcludeMachineOverride,
    "prefer_machine": PreferMachineOverride,
    "split": SplitOverride,
    "hold": HoldOverride,
}

# Every model type with a minimal valid payload. One row per public Pydantic class.
MINIMAL: list[tuple[type[BaseModel], Callable[[], Data]]] = [
    (SolveRequest, request),
    (Problem, problem),
    (Job, job),
    (Operation, operation),
    (ProcessingOption, option),
    (Precedence, lambda: {"operation_id": "o0"}),
    (OperatorNeed, lambda: {"pool_id": "pool1", "quantity": 1, "phase": "run"}),
    (Pin, lambda: {"machine_id": "m1"}),
    (OperationStatus, lambda: {"state": "pending"}),
    (Machine, machine),
    (MachineState, lambda: {}),
    (BatchCapacity, lambda: {"capacity": 10.0, "unit": "kg"}),
    (RestartRule, lambda: {"idle_threshold_seconds": 600.0, "warmup_seconds": 60.0}),
    (WorkCenter, work_center),
    (Calendar, calendar),
    (OperatorPool, lambda: {"id": "pool1", "size": 1}),
    (SetupFunction, setup_function),
    (AbsDiffTerm, abs_diff),
    (MismatchTerm, mismatch),
    (FamilyMatrixEntry, lambda: {"from_family": "a", "to_family": "b", "seconds": 5.0}),
    *[(OVERRIDE_CLASSES[kind], factory) for kind, factory in OVERRIDE_FACTORIES.items()],
    (FrozenZone, lambda: {"until": 100.0, "tolerance": 0.0}),
    (WeightedTerm, lambda: {"kind": "makespan", "weight": 1.0}),
    (WeightedSum, weighted_sum),
    (LexicographicLevel, lambda: {"kind": "makespan"}),
    (Lexicographic, lexicographic),
    (EpsilonBound, lambda: {"kind": "total_setup", "upper": 100.0}),
    (EpsilonConstraint, epsilon_constraint),
    (SchedulingProfile, profile),
    (CompiledProblem, lambda: {"problem": problem(), "profile": profile()}),
    (Assignment, assignment),
    (Schedule, schedule),
    (ScheduleIssue, issue),
    (PreviousAssignment, previous_assignment),
    (PreviousPlan, previous_plan),
    (VerificationResult, lambda: {"valid": True, "issues": [issue()]}),
    (SolveResult, lambda: {"schedule": schedule(), "verified": True}),
]
MINIMAL_IDS = [cls.__name__ for cls, _ in MINIMAL]


# --- helpers --------------------------------------------------------------------------------


def with_(base: Data, **changes: Any) -> Data:
    """Return a copy of a payload with top-level fields replaced."""
    return {**base, **changes}


def validate(target: Target, data: Data) -> Any:
    if isinstance(target, TypeAdapter):
        return target.validate_python(data)
    return target.model_validate(data)


def assert_rejected(target: Target, data: Data, field: str | int | None = None) -> None:
    """Assert validation fails; when `field` is given, some error must point at it."""
    with pytest.raises(ValidationError) as caught:
        validate(target, data)
    if field is not None:
        locations = [err["loc"] for err in caught.value.errors()]
        assert any(field in loc for loc in locations), locations


def request_with_operation(**changes: Any) -> Data:
    payload = request()
    payload["problem"]["jobs"][0]["operations"][0].update(changes)
    return payload


# --- module constants and schema hook ---------------------------------------------------------


def test_schema_version_constant_is_2_0() -> None:
    assert SCHEMA_VERSION == "2.0"


def test_request_json_schema_describes_solve_request() -> None:
    schema = request_json_schema()
    assert isinstance(schema, dict)
    assert schema["title"] == "SolveRequest"
    assert set(schema["properties"]) == {
        "schema_version",
        "problem",
        "profile",
        "overrides",
        "previous_plan",
    }
    assert "2.0" in json.dumps(schema["properties"]["schema_version"])
    assert json.loads(json.dumps(schema)) == schema
    assert request_json_schema() == schema


def test_objective_kind_has_exactly_the_thirteen_req_068_values_in_order() -> None:
    assert [kind.value for kind in ObjectiveKind] == [
        "makespan",
        "weighted_completion",
        "max_lateness",
        "tardy_count",
        "weighted_tardy_count",
        "total_tardiness",
        "weighted_tardiness",
        "earliness_tardiness",
        "total_setup",
        "cost",
        "churn",
        "robustness",
        "soft_penalty",
    ]
    assert ObjectiveKind("makespan") == "makespan"


# --- generic contract: every type ---------------------------------------------------------


@pytest.mark.parametrize(("cls", "factory"), MINIMAL, ids=MINIMAL_IDS)
def test_model_minimal_payload_validates(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    assert isinstance(cls.model_validate(factory()), cls)


@pytest.mark.parametrize(("cls", "factory"), MINIMAL, ids=MINIMAL_IDS)
def test_model_json_round_trip_is_identity(
    cls: type[BaseModel], factory: Callable[[], Data]
) -> None:
    original = cls.model_validate(factory())
    assert cls.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(("cls", "factory"), MINIMAL, ids=MINIMAL_IDS)
def test_model_unknown_field_rejected(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    assert_rejected(cls, with_(factory(), bogus=1), "bogus")


@pytest.mark.parametrize(("cls", "factory"), MINIMAL, ids=MINIMAL_IDS)
def test_model_is_frozen(cls: type[BaseModel], factory: Callable[[], Data]) -> None:
    instance = cls.model_validate(factory())
    field = next(iter(cls.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))


def test_nested_unknown_field_rejected_through_solve_request() -> None:
    assert_rejected(SolveRequest, request_with_operation(bogus=1), "bogus")


def test_v1_problem_body_rejected() -> None:
    v1 = {"schema_version": "2.0", "problem": {"operations": [], "resources": []}}
    assert_rejected(SolveRequest, v1, "operations")


def test_full_request_round_trips_with_every_override_and_previous_plan() -> None:
    payload = with_(
        request(),
        profile=with_(profile(), frozen_zone={"until": 50.0, "tolerance": 1.0}),
        overrides=[factory() for factory in OVERRIDE_FACTORIES.values()],
        previous_plan=previous_plan(),
    )
    original = SolveRequest.model_validate(payload)
    assert SolveRequest.model_validate_json(original.model_dump_json()) == original
    assert [type(ov) for ov in original.overrides] == list(OVERRIDE_CLASSES.values())


# --- SolveRequest -----------------------------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "2", "2.1", 2.0, None])
def test_solve_request_schema_version_other_than_2_0_rejected(version: Any) -> None:
    assert_rejected(SolveRequest, with_(request(), schema_version=version), "schema_version")


def test_solve_request_schema_version_missing_rejected() -> None:
    payload = request()
    del payload["schema_version"]
    assert_rejected(SolveRequest, payload, "schema_version")


def test_solve_request_defaults_profile_overrides_previous_plan() -> None:
    parsed = SolveRequest.model_validate(request())
    assert isinstance(parsed.profile, SchedulingProfile)
    assert list(parsed.overrides) == []
    assert parsed.previous_plan is None


def test_solve_request_more_than_10000_overrides_rejected() -> None:
    one = OVERRIDE.validate_python(hold())
    assert_rejected(SolveRequest, with_(request(), overrides=[one] * 10_001), "overrides")


def test_solve_request_10000_overrides_accepted() -> None:
    one = OVERRIDE.validate_python(hold())
    parsed = SolveRequest.model_validate(with_(request(), overrides=[one] * 10_000))
    assert len(parsed.overrides) == 10_000


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_solve_request_json_non_finite_number_rejected(bad: str) -> None:
    text = json.dumps(request()).replace('"quantity": 1.0', f'"quantity": {bad}')
    assert bad in text
    with pytest.raises(ValidationError):
        SolveRequest.model_validate_json(text)


# --- size bounds (REQ-084), built from pre-validated instances so they stay fast ------------


def _job_with_ops(job_id: str, count: int) -> Job:
    op = Operation.model_validate(operation())
    return Job.model_validate({"id": job_id, "operations": [op] * count})


def test_problem_more_than_100000_operations_total_rejected() -> None:
    jobs = [_job_with_ops("a", 50_001), _job_with_ops("b", 50_000)]
    with pytest.raises(ValidationError):
        Problem.model_validate(with_(problem(), jobs=jobs))


def test_problem_exactly_100000_operations_total_accepted() -> None:
    jobs = [_job_with_ops("a", 50_000), _job_with_ops("b", 50_000)]
    parsed = Problem.model_validate(with_(problem(), jobs=jobs))
    assert sum(len(j.operations) for j in parsed.jobs) == 100_000


def test_problem_more_than_10000_machines_rejected() -> None:
    one = Machine.model_validate(machine())
    assert_rejected(Problem, with_(problem(), machines=[one] * 10_001), "machines")


def test_problem_10000_machines_accepted() -> None:
    one = Machine.model_validate(machine())
    assert len(Problem.model_validate(with_(problem(), machines=[one] * 10_000)).machines) == 10_000


def test_problem_more_than_1000000_calendar_intervals_total_rejected() -> None:
    first = Calendar.model_construct(
        id="c1", intervals=[(float(i), i + 0.5) for i in range(500_000)]
    )
    second = Calendar.model_construct(
        id="c2", intervals=[(float(i), i + 0.5) for i in range(500_001)]
    )
    with pytest.raises(ValidationError):
        Problem.model_validate(with_(problem(), calendars=[first, second]))


# --- ids and text -----------------------------------------------------------------------------

ID_CASES: list[tuple[Target, Callable[[], Data], str]] = [
    (Problem, problem, "id"),
    (Job, job, "id"),
    (Operation, operation, "id"),
    (Operation, operation, "work_center_id"),
    (ProcessingOption, option, "machine_id"),
    (Precedence, lambda: {"operation_id": "o0"}, "operation_id"),
    (Machine, machine, "id"),
    (Machine, machine, "work_center_id"),
    (WorkCenter, work_center, "id"),
    (Calendar, calendar, "id"),
    (OperatorPool, lambda: {"id": "pool1", "size": 1}, "id"),
    (SetupFunction, setup_function, "id"),
    (Assignment, assignment, "operation_id"),
    (OVERRIDE, lock, "id"),
    (OVERRIDE, lock, "operation_id"),
    (OVERRIDE, expedite, "job_id"),
    (PreviousPlan, previous_plan, "plan_id"),
]
ID_CASE_IDS = [f"{getattr(t, '__name__', 'Override')}.{f}" for t, _, f in ID_CASES]


@pytest.mark.parametrize(("target", "factory", "field"), ID_CASES, ids=ID_CASE_IDS)
def test_id_over_200_chars_rejected(
    target: Target, factory: Callable[[], Data], field: str
) -> None:
    assert_rejected(target, with_(factory(), **{field: "x" * (ID_LIMIT + 1)}), field)


@pytest.mark.parametrize(("target", "factory", "field"), ID_CASES, ids=ID_CASE_IDS)
def test_id_exactly_200_chars_accepted(
    target: Target, factory: Callable[[], Data], field: str
) -> None:
    validate(target, with_(factory(), **{field: "x" * ID_LIMIT}))


@pytest.mark.parametrize(("target", "factory", "field"), ID_CASES, ids=ID_CASE_IDS)
def test_id_empty_rejected(target: Target, factory: Callable[[], Data], field: str) -> None:
    assert_rejected(target, with_(factory(), **{field: ""}), field)


TEXT_CASES: list[tuple[Target, Callable[[], Data], str]] = [
    (Job, job, "customer"),
    (OVERRIDE, hold, "reason"),
    (OVERRIDE, hold, "fact_ref"),
    (ScheduleIssue, issue, "message"),
    (ScheduleIssue, issue, "path"),
]
TEXT_CASE_IDS = [f"{getattr(t, '__name__', 'Override')}.{f}" for t, _, f in TEXT_CASES]


@pytest.mark.parametrize(("target", "factory", "field"), TEXT_CASES, ids=TEXT_CASE_IDS)
def test_text_over_2000_chars_rejected(
    target: Target, factory: Callable[[], Data], field: str
) -> None:
    assert_rejected(target, with_(factory(), **{field: "x" * (TEXT_LIMIT + 1)}), field)


@pytest.mark.parametrize(("target", "factory", "field"), TEXT_CASES, ids=TEXT_CASE_IDS)
def test_text_exactly_2000_chars_accepted(
    target: Target, factory: Callable[[], Data], field: str
) -> None:
    validate(target, with_(factory(), **{field: "x" * TEXT_LIMIT}))


def test_setup_param_string_value_over_2000_chars_rejected() -> None:
    assert_rejected(Operation, with_(operation(), setup_params={"color": "x" * 2001}), "color")


# --- non-finite floats everywhere -------------------------------------------------------------


def _profile_target(value: float) -> Data:
    return with_(profile(), robustness_load_targets=[value])


FLOAT_FIELDS: list[tuple[Target, Callable[[], Data], str]] = [
    (Problem, problem, "now"),
    (Problem, problem, "horizon_end"),
    (Job, job, "release"),
    (Job, job, "due"),
    (Job, job, "promised_ship"),
    (Job, job, "weight_override"),
    (Operation, operation, "quantity"),
    (Operation, operation, "ready_time"),
    (ProcessingOption, option, "fixed_seconds"),
    (ProcessingOption, option, "unit_seconds"),
    (ProcessingOption, option, "preference_cost"),
    (Precedence, lambda: {"operation_id": "o0"}, "min_lag"),
    (Precedence, lambda: {"operation_id": "o0"}, "max_lag"),
    (Precedence, lambda: {"operation_id": "o0"}, "overlap_quantity"),
    (Pin, lambda: {}, "earliest_start"),
    (Pin, lambda: {}, "latest_start"),
    (OperationStatus, lambda: {"state": "pending"}, "remaining_seconds"),
    (Machine, machine, "hour_cost"),
    (Machine, machine, "setup_hour_cost"),
    (MachineState, lambda: {}, "available_from"),
    (BatchCapacity, lambda: {"capacity": 10.0, "unit": "kg"}, "capacity"),
    (RestartRule, lambda: {"idle_threshold_seconds": 1.0, "warmup_seconds": 1.0}, "warmup_seconds"),
    (SetupFunction, setup_function, "default_seconds"),
    (SETUP_TERM, abs_diff, "seconds_per_unit"),
    (SETUP_TERM, mismatch, "seconds"),
    (FamilyMatrixEntry, lambda: {"from_family": "a", "to_family": "b", "seconds": 1.0}, "seconds"),
    (FrozenZone, lambda: {"until": 1.0, "tolerance": 0.0}, "until"),
    (FrozenZone, lambda: {"until": 1.0, "tolerance": 0.0}, "tolerance"),
    (WeightedTerm, lambda: {"kind": "makespan", "weight": 1.0}, "weight"),
    (WeightedTerm, lambda: {"kind": "makespan", "weight": 1.0}, "scale"),
    (LexicographicLevel, lambda: {"kind": "makespan"}, "tolerance"),
    (EpsilonBound, lambda: {"kind": "cost", "upper": 1.0}, "upper"),
    (SchedulingProfile, profile, "time_budget_seconds"),
    (SchedulingProfile, profile, "horizon_end"),
    (Assignment, assignment, "start"),
    (Assignment, assignment, "setup_end"),
    (Assignment, assignment, "end"),
    (Schedule, schedule, "primary_value"),
    (Schedule, schedule, "best_bound"),
    (Schedule, schedule, "elapsed_seconds"),
    (PreviousAssignment, previous_assignment, "start"),
    (OVERRIDE, lock, "start"),
    (OVERRIDE, expedite, "due"),
    (OVERRIDE, lambda: _override("priority", job_id="j1", weight=2.0), "weight"),
    (OVERRIDE, lambda: with_(exclude_machine(), hardness="soft"), "penalty"),
    (OVERRIDE, lambda: with_(prefer_machine(), hardness="soft"), "penalty"),
    (OVERRIDE, hold, "until"),
]
FLOAT_CASES = [(t, f, name, v) for t, f, name in FLOAT_FIELDS for v in NON_FINITE]
FLOAT_CASE_IDS = [f"{getattr(t, '__name__', 'Union')}.{name}-{v}" for t, _, name, v in FLOAT_CASES]


@pytest.mark.parametrize(("target", "factory", "field", "value"), FLOAT_CASES, ids=FLOAT_CASE_IDS)
def test_non_finite_float_rejected(
    target: Target, factory: Callable[[], Data], field: str, value: float
) -> None:
    assert_rejected(target, with_(factory(), **{field: value}), field)


NESTED_NON_FINITE: list[tuple[str, Target, Callable[[float], Data], str]] = [
    ("calendar_interval", Calendar, lambda v: with_(calendar(), intervals=[[0.0, v]]), "intervals"),
    ("setup_param", Operation, lambda v: with_(operation(), setup_params={"w": v}), "w"),
    (
        "machine_state_param",
        MachineState,
        lambda v: {"last_setup_params": {"w": v}},
        "w",
    ),
    ("split_fraction", OVERRIDE, lambda v: with_(split(), fractions=[v, 0.5]), "fractions"),
    ("robustness_target", SchedulingProfile, _profile_target, "robustness_load_targets"),
    ("class_weight", SchedulingProfile, lambda v: with_(profile(), class_weights={1: v}), 1),
    (
        "objective_value",
        SolveResult,
        lambda v: {"schedule": schedule(), "verified": True, "objective_values": {"makespan": v}},
        "objective_values",
    ),
]


@pytest.mark.parametrize("value", NON_FINITE, ids=str)
@pytest.mark.parametrize(
    ("target", "build", "field"),
    [case[1:] for case in NESTED_NON_FINITE],
    ids=[case[0] for case in NESTED_NON_FINITE],
)
def test_nested_non_finite_float_rejected(
    target: Target, build: Callable[[float], Data], field: str | int, value: float
) -> None:
    assert_rejected(target, build(value), field)


# --- numeric bounds ---------------------------------------------------------------------------

BOUND_CASES: list[tuple[str, Target, Data, str | int | None]] = [
    ("weight_class_3", Job, with_(job(), weight_class=3), "weight_class"),
    ("weight_class_0", Job, with_(job(), weight_class=0), "weight_class"),
    ("weight_override_0", Job, with_(job(), weight_override=0.0), "weight_override"),
    ("job_status_unknown", Job, with_(job(), status="cancelled"), "status"),
    ("quantity_0", Operation, with_(operation(), quantity=0.0), "quantity"),
    ("quantity_negative", Operation, with_(operation(), quantity=-1.0), "quantity"),
    ("options_empty", Operation, with_(operation(), options=[]), "options"),
    ("shift_crossing_unknown", Operation, with_(operation(), shift_crossing="split"), None),
    ("fixed_negative", ProcessingOption, with_(option(), fixed_seconds=-1.0), "fixed_seconds"),
    ("unit_negative", ProcessingOption, with_(option(), unit_seconds=-0.1), "unit_seconds"),
    (
        "preference_negative",
        ProcessingOption,
        with_(option(), preference_cost=-1.0),
        "preference_cost",
    ),
    ("min_lag_negative", Precedence, {"operation_id": "o0", "min_lag": -1.0}, "min_lag"),
    ("max_lag_negative", Precedence, {"operation_id": "o0", "max_lag": -1.0}, None),
    (
        "max_lag_below_min_lag",
        Precedence,
        {"operation_id": "o0", "min_lag": 10.0, "max_lag": 5.0},
        None,
    ),
    (
        "overlap_quantity_0",
        Precedence,
        {"operation_id": "o0", "overlap_quantity": 0.0},
        "overlap_quantity",
    ),
    ("operator_qty_0", OperatorNeed, {"pool_id": "p", "quantity": 0, "phase": "run"}, "quantity"),
    (
        "operator_qty_fraction",
        OperatorNeed,
        {"pool_id": "p", "quantity": 1.5, "phase": "run"},
        "quantity",
    ),
    ("operator_phase", OperatorNeed, {"pool_id": "p", "quantity": 1, "phase": "teardown"}, "phase"),
    ("op_status_unknown", OperationStatus, {"state": "paused"}, "state"),
    ("pool_size_0", OperatorPool, {"id": "pool1", "size": 0}, "size"),
    ("pool_size_negative", OperatorPool, {"id": "pool1", "size": -1}, "size"),
    ("hour_cost_negative", Machine, with_(machine(), hour_cost=-1.0), "hour_cost"),
    ("setup_cost_negative", Machine, with_(machine(), setup_hour_cost=-1.0), "setup_hour_cost"),
    ("batch_capacity_0", BatchCapacity, {"capacity": 0.0, "unit": "kg"}, "capacity"),
    (
        "restart_threshold_0",
        RestartRule,
        {"idle_threshold_seconds": 0.0, "warmup_seconds": 60.0},
        "idle_threshold_seconds",
    ),
    (
        "restart_threshold_negative",
        RestartRule,
        {"idle_threshold_seconds": -1.0, "warmup_seconds": 60.0},
        "idle_threshold_seconds",
    ),
    (
        "restart_warmup_0",
        RestartRule,
        {"idle_threshold_seconds": 600.0, "warmup_seconds": 0.0},
        "warmup_seconds",
    ),
    (
        "restart_warmup_negative",
        RestartRule,
        {"idle_threshold_seconds": 600.0, "warmup_seconds": -5.0},
        "warmup_seconds",
    ),
    (
        "setup_default_negative",
        SetupFunction,
        with_(setup_function(), default_seconds=-1.0),
        "default_seconds",
    ),
    ("setup_combine_unknown", SetupFunction, with_(setup_function(), combine="min"), "combine"),
    ("abs_diff_rate_negative", SETUP_TERM, with_(abs_diff(), seconds_per_unit=-1.0), None),
    ("mismatch_seconds_negative", SETUP_TERM, with_(mismatch(), seconds=-1.0), None),
    ("setup_term_unknown_kind", SETUP_TERM, {"kind": "formula", "param": "x"}, None),
    (
        "family_seconds_negative",
        FamilyMatrixEntry,
        {"from_family": "a", "to_family": "b", "seconds": -1.0},
        "seconds",
    ),
    ("frozen_tolerance_negative", FrozenZone, {"until": 1.0, "tolerance": -1.0}, "tolerance"),
    ("schedule_status_unknown", Schedule, with_(schedule(), status="DONE"), "status"),
]


@pytest.mark.parametrize(
    ("target", "data", "field"),
    [case[1:] for case in BOUND_CASES],
    ids=[case[0] for case in BOUND_CASES],
)
def test_bound_violation_rejected(target: Target, data: Data, field: str | int | None) -> None:
    assert_rejected(target, data, field)


@pytest.mark.parametrize("weight_class", [1, 2, 4])
def test_job_weight_class_allowed_values_accepted(weight_class: int) -> None:
    assert Job.model_validate(with_(job(), weight_class=weight_class)).weight_class == weight_class


def test_job_defaults() -> None:
    parsed = Job.model_validate(job())
    assert parsed.weight_class == 1
    assert parsed.weight_override is None
    assert parsed.release == 0
    assert parsed.status == "open"
    assert list(parsed.sales_order_refs) == []


def test_precedence_max_lag_equal_to_min_lag_accepted() -> None:
    parsed = Precedence.model_validate({"operation_id": "o0", "min_lag": 5.0, "max_lag": 5.0})
    assert parsed.max_lag == parsed.min_lag == 5.0


def test_operation_defaults() -> None:
    parsed = Operation.model_validate(operation())
    assert parsed.setup_detached is False
    assert parsed.shift_crossing == "forbid"
    assert parsed.status.state == "pending"
    assert parsed.pin is None
    assert parsed.ready_time == 0


def test_operation_setup_params_keep_numbers_and_strings() -> None:
    parsed = Operation.model_validate(with_(operation(), setup_params={"w": 3.5, "color": "red"}))
    assert dict(parsed.setup_params) == {"w": 3.5, "color": "red"}


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, None])
def test_operation_setup_detached_non_bool_rejected(value: Any) -> None:
    assert_rejected(Operation, with_(operation(), setup_detached=value), "setup_detached")


def test_operation_setup_detached_json_string_rejected() -> None:
    text = json.dumps(with_(operation(), setup_detached="true"))
    with pytest.raises(ValidationError):
        Operation.model_validate_json(text)


def test_operation_setup_detached_true_accepted() -> None:
    assert Operation.model_validate(with_(operation(), setup_detached=True)).setup_detached is True


def test_machine_defaults() -> None:
    parsed = Machine.model_validate(machine())
    assert parsed.calendar_id is None
    assert parsed.restart is None
    assert parsed.hour_cost == 0
    assert parsed.state.available_from == 0


def test_operator_pool_size_1_accepted() -> None:
    assert OperatorPool.model_validate({"id": "pool1", "size": 1}).size == 1


# --- calendars --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "intervals",
    [
        [[20.0, 30.0], [0.0, 10.0]],
        [[0.0, 10.0], [5.0, 15.0]],
        [[0.0, 10.0], [0.0, 10.0]],
        [[5.0, 5.0]],
        [[6.0, 5.0]],
    ],
    ids=["unsorted", "overlapping", "duplicate", "zero_length", "start_after_end"],
)
def test_calendar_bad_intervals_rejected(intervals: list[list[float]]) -> None:
    assert_rejected(Calendar, with_(calendar(), intervals=intervals))


@pytest.mark.parametrize(
    "intervals",
    [[], [[0.0, 10.0], [10.0, 20.0]], [[-5.0, 0.0]]],
    ids=["empty", "touching", "negative_start"],
)
def test_calendar_valid_intervals_accepted(intervals: list[list[float]]) -> None:
    Calendar.model_validate(with_(calendar(), intervals=intervals))


# --- assignments ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "setup_end", "end"),
    [(5.0, 4.0, 10.0), (0.0, 6.0, 5.0), (10.0, 10.0, 9.0)],
    ids=["setup_end_before_start", "end_before_setup_end", "end_before_start"],
)
def test_assignment_out_of_order_rejected(start: float, setup_end: float, end: float) -> None:
    assert_rejected(Assignment, with_(assignment(), start=start, setup_end=setup_end, end=end))


def test_assignment_equal_times_accepted() -> None:
    parsed = Assignment.model_validate(with_(assignment(), start=3.0, setup_end=3.0, end=3.0))
    assert parsed.batch_id is None


# --- overrides --------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(OVERRIDE_FACTORIES))
def test_override_union_dispatches_on_kind(kind: str) -> None:
    parsed = OVERRIDE.validate_python(OVERRIDE_FACTORIES[kind]())
    assert type(parsed) is OVERRIDE_CLASSES[kind]
    assert parsed.kind == kind
    assert (parsed.hardness, parsed.fact_ref, parsed.reason) == ("hard", "fact:1", "customer call")


@pytest.mark.parametrize("kind", list(OVERRIDE_FACTORIES))
def test_override_round_trip_through_union(kind: str) -> None:
    parsed = OVERRIDE.validate_python(OVERRIDE_FACTORIES[kind]())
    assert OVERRIDE.validate_json(OVERRIDE.dump_json(parsed)) == parsed


def test_override_unknown_kind_rejected() -> None:
    assert_rejected(OVERRIDE, with_(hold(), kind="teleport"))


def test_override_missing_kind_rejected() -> None:
    payload = hold()
    del payload["kind"]
    assert_rejected(OVERRIDE, payload)


@pytest.mark.parametrize("kind", list(OVERRIDE_FACTORIES))
@pytest.mark.parametrize("field", ["fact_ref", "reason"])
def test_override_empty_fact_ref_or_reason_rejected(kind: str, field: str) -> None:
    assert_rejected(OVERRIDE, with_(OVERRIDE_FACTORIES[kind](), **{field: ""}), field)


@pytest.mark.parametrize("kind", list(OVERRIDE_FACTORIES))
@pytest.mark.parametrize("field", ["id", "fact_ref", "reason", "hardness"])
def test_override_missing_common_field_rejected(kind: str, field: str) -> None:
    payload = OVERRIDE_FACTORIES[kind]()
    del payload[field]
    assert_rejected(OVERRIDE, payload, field)


@pytest.mark.parametrize("kind", ["lock", "expedite", "priority", "split", "hold"])
def test_override_hard_only_kinds_reject_soft(kind: str) -> None:
    assert_rejected(OVERRIDE, with_(OVERRIDE_FACTORIES[kind](), hardness="soft"), "hardness")


@pytest.mark.parametrize("kind", ["exclude_machine", "prefer_machine"])
def test_override_machine_kinds_accept_soft_with_penalty(kind: str) -> None:
    payload = with_(OVERRIDE_FACTORIES[kind](), hardness="soft", penalty=50.0)
    parsed = OVERRIDE.validate_python(payload)
    assert (parsed.hardness, parsed.penalty) == ("soft", 50.0)


@pytest.mark.parametrize("kind", list(OVERRIDE_FACTORIES))
def test_override_hardness_unknown_rejected(kind: str) -> None:
    assert_rejected(OVERRIDE, with_(OVERRIDE_FACTORIES[kind](), hardness="firm"), "hardness")


@pytest.mark.parametrize("kind", ["exclude_machine", "prefer_machine"])
def test_override_penalty_negative_rejected(kind: str) -> None:
    payload = with_(OVERRIDE_FACTORIES[kind](), hardness="soft", penalty=-1.0)
    assert_rejected(OVERRIDE, payload, "penalty")


@pytest.mark.parametrize("kind", ["exclude_machine", "prefer_machine"])
def test_override_machine_kinds_need_exactly_one_of_operation_or_job(kind: str) -> None:
    base = {
        k: v for k, v in OVERRIDE_FACTORIES[kind]().items() if k not in ("operation_id", "job_id")
    }
    assert_rejected(OVERRIDE, base)
    assert_rejected(OVERRIDE, with_(base, operation_id="o1", job_id="j1"))
    OVERRIDE.validate_python(with_(base, operation_id="o1"))
    OVERRIDE.validate_python(with_(base, job_id="j1"))


def test_override_priority_needs_exactly_one_of_weight_class_or_weight() -> None:
    base = {k: v for k, v in priority().items() if k != "weight_class"}
    assert_rejected(OVERRIDE, base)
    assert_rejected(OVERRIDE, with_(base, weight_class=2, weight=3.0))
    assert OVERRIDE.validate_python(with_(base, weight=3.0)).weight == 3.0
    assert_rejected(OVERRIDE, with_(base, weight_class=3), "weight_class")
    assert_rejected(OVERRIDE, with_(base, weight=0.0), "weight")


def test_override_lock_machine_and_start_optional() -> None:
    payload = {k: v for k, v in lock().items() if k not in ("machine_id", "start")}
    parsed = OVERRIDE.validate_python(payload)
    assert (parsed.machine_id, parsed.start) == (None, None)


def test_override_hold_until_optional() -> None:
    payload = {k: v for k, v in hold().items() if k != "until"}
    assert OVERRIDE.validate_python(payload).until is None


@pytest.mark.parametrize(
    "fractions",
    [[0.5, 0.5], [0.25, 0.25, 0.5], [0.1] * 10],
    ids=["halves", "quarters", "float_tenths"],
)
def test_override_split_fractions_summing_to_1_accepted(fractions: list[float]) -> None:
    parsed = OVERRIDE.validate_python(with_(split(), fractions=fractions))
    assert list(parsed.fractions) == fractions


@pytest.mark.parametrize(
    "fractions",
    [[0.5, 0.4], [0.6, 0.6], [1.5, -0.5], [0.0, 1.0], [-0.2, 1.2], []],
    ids=["sum_below", "sum_above", "negative", "zero_part", "negative_sum_1", "empty"],
)
def test_override_split_bad_fractions_rejected(fractions: list[float]) -> None:
    assert_rejected(OVERRIDE, with_(split(), fractions=fractions))


# --- objective specs --------------------------------------------------------------------------

SPEC_FACTORIES: dict[str, tuple[Callable[[], Data], type[BaseModel]]] = {
    "weighted_sum": (weighted_sum, WeightedSum),
    "lexicographic": (lexicographic, Lexicographic),
    "epsilon_constraint": (epsilon_constraint, EpsilonConstraint),
}


@pytest.mark.parametrize("mode", list(SPEC_FACTORIES))
def test_objective_spec_union_dispatches_on_mode(mode: str) -> None:
    factory, cls = SPEC_FACTORIES[mode]
    parsed = OBJECTIVE_SPEC.validate_python(factory())
    assert type(parsed) is cls
    assert parsed.mode == mode
    assert OBJECTIVE_SPEC.validate_json(OBJECTIVE_SPEC.dump_json(parsed)) == parsed


def test_objective_spec_unknown_mode_rejected() -> None:
    assert_rejected(OBJECTIVE_SPEC, with_(lexicographic(), mode="pareto"))


def test_objective_spec_unknown_kind_rejected() -> None:
    assert_rejected(OBJECTIVE_SPEC, {"mode": "lexicographic", "levels": [{"kind": "profit"}]})


def test_weighted_sum_term_kinds_are_objective_kinds() -> None:
    parsed = WeightedSum.model_validate(weighted_sum())
    assert parsed.terms[0].kind is ObjectiveKind.MAKESPAN


def test_lexicographic_level_defaults_to_zero_absolute_tolerance() -> None:
    level = LexicographicLevel.model_validate({"kind": "makespan"})
    assert (level.tolerance, level.tolerance_kind) == (0.0, "absolute")
    relative = {"kind": "makespan", "tolerance": 0.05, "tolerance_kind": "relative"}
    assert LexicographicLevel.model_validate(relative).tolerance_kind == "relative"


OBJECTIVE_BOUND_CASES: list[tuple[str, Target, Data, str | None]] = [
    ("weight_negative", WeightedTerm, {"kind": "makespan", "weight": -1.0}, "weight"),
    ("scale_0", WeightedTerm, {"kind": "makespan", "weight": 1.0, "scale": 0.0}, "scale"),
    ("scale_negative", WeightedTerm, {"kind": "makespan", "weight": 1.0, "scale": -1.0}, "scale"),
    (
        "tolerance_negative",
        LexicographicLevel,
        {"kind": "makespan", "tolerance": -0.1},
        "tolerance",
    ),
    (
        "tolerance_kind_unknown",
        LexicographicLevel,
        {"kind": "makespan", "tolerance_kind": "fuzzy"},
        "tolerance_kind",
    ),
    ("weighted_sum_no_terms", WeightedSum, {"mode": "weighted_sum", "terms": []}, "terms"),
    ("lexicographic_no_levels", Lexicographic, {"mode": "lexicographic", "levels": []}, "levels"),
    (
        "epsilon_primary_unknown",
        EpsilonConstraint,
        with_(epsilon_constraint(), primary="profit"),
        "primary",
    ),
]


@pytest.mark.parametrize(
    ("target", "data", "field"),
    [case[1:] for case in OBJECTIVE_BOUND_CASES],
    ids=[case[0] for case in OBJECTIVE_BOUND_CASES],
)
def test_objective_spec_bound_violation_rejected(
    target: Target, data: Data, field: str | None
) -> None:
    assert_rejected(target, data, field)


def test_weighted_term_weight_0_accepted() -> None:
    assert WeightedTerm.model_validate({"kind": "cost", "weight": 0.0}).scale is None


# --- scheduling profile -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "targets",
    [[0.9, 0.95, 0.7], [0.0, 0.0, 0.0], [1.1, 0.8, 0.7], [0.9, 0.8, -0.1], [0.7, 0.8, 0.9]],
    ids=["increasing_middle", "zero", "above_1", "negative", "ascending"],
)
def test_profile_bad_robustness_targets_rejected(targets: list[float]) -> None:
    assert_rejected(SchedulingProfile, with_(profile(), robustness_load_targets=targets))


@pytest.mark.parametrize(
    "targets",
    [[1.0, 1.0, 0.5], [1.0], [0.9, 0.8, 0.7]],
    ids=["equal_neighbors", "single_1", "default"],
)
def test_profile_valid_robustness_targets_accepted(targets: list[float]) -> None:
    parsed = SchedulingProfile.model_validate(with_(profile(), robustness_load_targets=targets))
    assert list(parsed.robustness_load_targets) == targets


@pytest.mark.parametrize("budget", [0.0, -1.0, 300.0001, 301.0])
def test_profile_time_budget_outside_0_300_rejected(budget: float) -> None:
    assert_rejected(
        SchedulingProfile, with_(profile(), time_budget_seconds=budget), "time_budget_seconds"
    )


@pytest.mark.parametrize("budget", [0.001, 300.0])
def test_profile_time_budget_inside_0_300_accepted(budget: float) -> None:
    parsed = SchedulingProfile.model_validate(with_(profile(), time_budget_seconds=budget))
    assert parsed.time_budget_seconds == budget


# --- method params (T-114, REQ-087, D-070) ---------------------------------------------------

ATCS_PARAMS = {"atcs": {"K1": 5.0, "flexibility": True, "mode": "active", "iters": 3}}


def test_profile_method_params_default_to_an_empty_mapping() -> None:
    assert SchedulingProfile.model_validate(profile()).method_params == {}
    assert SchedulingProfile().method_params == {}


def test_profile_method_params_accept_scalar_values_and_keep_their_types() -> None:
    parsed = SchedulingProfile.model_validate(with_(profile(), method_params=ATCS_PARAMS))
    atcs = parsed.method_params["atcs"]
    assert atcs == ATCS_PARAMS["atcs"]
    assert (type(atcs["K1"]), type(atcs["flexibility"]), type(atcs["iters"])) == (
        float,
        bool,
        int,
    )
    assert atcs["mode"] == "active"


@pytest.mark.parametrize(
    "value", [[1.0, 2.0], {"nested": 1.0}, None], ids=["list", "mapping", "none"]
)
def test_profile_method_params_reject_a_non_scalar_value(value: Any) -> None:
    data = with_(profile(), method_params={"atcs": {"K1": value}})
    with pytest.raises(ValidationError) as caught:
        SchedulingProfile.model_validate(data)
    locations = [err["loc"][:3] for err in caught.value.errors()]
    # The error points inside the params, so the field exists and checks its values.
    assert ("method_params", "atcs", "K1") in locations, locations


def test_profile_class_weight_key_3_rejected() -> None:
    assert_rejected(
        SchedulingProfile, with_(profile(), class_weights={1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0})
    )


def test_profile_class_weight_zero_rejected() -> None:
    assert_rejected(SchedulingProfile, with_(profile(), class_weights={1: 0.0, 2: 2.0, 4: 4.0}))


def test_profile_objective_accepts_each_spec_variant() -> None:
    for mode, (factory, cls) in SPEC_FACTORIES.items():
        parsed = SchedulingProfile.model_validate(with_(profile(), objective=factory()))
        assert type(parsed.objective) is cls, mode


def test_profile_frozen_zone_and_horizon_optional() -> None:
    parsed = SchedulingProfile.model_validate(profile())
    assert (parsed.frozen_zone, parsed.horizon_end) == (None, None)


# --- results ----------------------------------------------------------------------------------


def test_compiled_problem_defaults_empty_bookkeeping() -> None:
    parsed = CompiledProblem.model_validate({"problem": problem(), "profile": profile()})
    assert list(parsed.held_job_ids) == []
    assert list(parsed.released_from_frozen) == []
    assert dict(parsed.split_map) == {}
    assert list(parsed.override_issues) == []


def test_compiled_problem_full_round_trip() -> None:
    payload = {
        "problem": problem(),
        "profile": profile(),
        "held_job_ids": ["j9"],
        "released_from_frozen": ["o7"],
        "split_map": {"j1": ["j1~1", "j1~2"]},
        "override_issues": [issue()],
    }
    original = CompiledProblem.model_validate(payload)
    assert CompiledProblem.model_validate_json(original.model_dump_json()) == original


def test_schedule_defaults() -> None:
    parsed = Schedule.model_validate(schedule())
    assert (parsed.primary_value, parsed.best_bound) == (None, None)
    assert list(parsed.issues) == []


def test_solve_result_full_round_trip_keys_objective_values_by_kind() -> None:
    payload = {
        "schedule": schedule(),
        "verified": False,
        "objective_values": {kind.value: 1.0 for kind in ObjectiveKind},
        "issues": [issue()],
        "held_job_ids": ["j2"],
        "released_from_frozen": ["o3"],
        "features": ["setups", "calendars"],
    }
    original = SolveResult.model_validate(payload)
    assert set(original.objective_values) == set(ObjectiveKind)
    assert SolveResult.model_validate_json(original.model_dump_json()) == original


def test_solve_result_unknown_objective_kind_rejected() -> None:
    payload = {"schedule": schedule(), "verified": True, "objective_values": {"profit": 1.0}}
    assert_rejected(SolveResult, payload, "objective_values")


# --- ATCS run records (T-130, REQ-121, SDD Data Model "AtcsRun", "Schedule additions") ------
#
# `AtcsRun` is read through the module attribute (not the import block above) so that, before
# it exists, only these tests fail rather than the whole file.


def atcs_run() -> Data:
    return {
        "work_center_id": "WC-M1",
        "tau": 0.566,
        "R": 0.472,
        "eta": 0.178,
        "m": 1,
        "K1": 4.97,
        "K2": 0.671,
        "source": "formula",
        "objective_value": 98.0,
    }


def _atcs_run_model() -> type[BaseModel]:
    cls: type[BaseModel] = model_module.AtcsRun
    return cls


def test_atcs_run_full_payload_validates_with_every_field() -> None:
    parsed = _atcs_run_model().model_validate(atcs_run())
    assert parsed.model_dump() == atcs_run()


def test_atcs_run_json_round_trip_is_identity() -> None:
    cls = _atcs_run_model()
    original = cls.model_validate(atcs_run())
    assert cls.model_validate_json(original.model_dump_json()) == original


def test_atcs_run_is_frozen_and_closed() -> None:
    cls = _atcs_run_model()
    assert_rejected(cls, with_(atcs_run(), bogus=1), "bogus")
    instance = cls.model_validate(atcs_run())
    with pytest.raises(ValidationError):
        instance.K1 = 1.0


@pytest.mark.parametrize("source", ["formula", "table", "explicit", "perturbed"])
def test_atcs_run_accepts_each_k_source(source: str) -> None:
    parsed = _atcs_run_model().model_validate(with_(atcs_run(), source=source))
    assert parsed.source == source


def test_atcs_run_unknown_k_source_rejected() -> None:
    assert_rejected(_atcs_run_model(), with_(atcs_run(), source="guess"), "source")


def test_atcs_run_objective_value_may_be_none() -> None:
    parsed = _atcs_run_model().model_validate(with_(atcs_run(), objective_value=None))
    assert parsed.objective_value is None


def test_atcs_run_machine_count_is_an_int() -> None:
    assert_rejected(_atcs_run_model(), with_(atcs_run(), m=1.5), "m")


@pytest.mark.parametrize("field", ["tau", "R", "eta", "K1", "K2"])
def test_atcs_run_non_finite_factor_rejected(field: str) -> None:
    assert_rejected(_atcs_run_model(), with_(atcs_run(), **{field: math.inf}), field)


def test_schedule_atcs_runs_default_to_empty() -> None:
    parsed = Schedule.model_validate(schedule())
    assert parsed.atcs_runs == ()


def test_schedule_with_atcs_runs_round_trips() -> None:
    payload = with_(schedule(), atcs_runs=[atcs_run(), with_(atcs_run(), work_center_id="B")])
    original = Schedule.model_validate(payload)
    runs = original.atcs_runs
    assert isinstance(runs, tuple)
    assert [r.work_center_id for r in runs] == ["WC-M1", "B"]
    assert all(isinstance(r, _atcs_run_model()) for r in runs)
    assert Schedule.model_validate_json(original.model_dump_json()) == original
