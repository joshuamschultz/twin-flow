"""Cases absorbed from the deleted production package (T-109, COMP-015, REQ-080).

Each test re-expresses one case of `tests/production/` against problem model v2 and cites
its source in its docstring. Production concepts map to v2 as follows:

- integer `Resource.capacity` -> several unary machines in one work center (D-062);
- `Resource.windows` -> a machine `Calendar` (D-065);
- `RecipeAlternative` with `preference_cost` -> `ProcessingOption.preference_cost`, scored
  by the `soft_penalty` objective and `objectives.rank` (D-068);
- `Phase`/`ResourceUse` on an auxiliary resource -> `OperatorNeed.phase` on a pool (D-063);
- a setup `Phase` -> the setup phase `[start, setup_end)` from a `SetupFunction` (D-063, D-064);
- `ThermalRecipe`/`BatchRequirement` -> `Machine.batch` plus `Operation.batch_family`;
  batch membership is the shared `Assignment.batch_id`, there is no separate batch record.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from mutations import assignment_of, drop, join_batch, replace, shift, shorten_run, with_primary
from pydantic import ValidationError

from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.model import (
    Assignment,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    Operation,
    Problem,
    Schedule,
    SetupFunction,
    SolveRequest,
    SolveResult,
    WorkCenter,
)
from twinflow.scheduling.objectives import evaluate, rank
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

WC = "WC"
OVEN_CAPACITY = {"capacity": 100.0, "unit": "lb"}
HEAT_SECONDS = 70.0
"""Ramp 10 s + soak 60 s of the production thermal recipe, as one batch run."""


# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, *options: tuple[str, float, float], **fields: Any) -> Operation:
    """An operation of quantity 1 with `(machine_id, fixed_seconds, preference_cost)` options."""
    data = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": 1.0,
        "options": [
            {"machine_id": m, "fixed_seconds": s, "unit_seconds": 0.0, "preference_cost": c}
            for m, s, c in options
        ],
    }
    return Operation.model_validate(data | fields)


def _heat(op_id: str, quantity: float, family: str, oven: str = "oven", **fields: Any) -> Operation:
    """A batch operation of `quantity` lb on one oven; its run does not scale with quantity."""
    return _op(op_id, (oven, HEAT_SECONDS, 0.0), quantity=quantity, batch_family=family, **fields)


def _after(*op_ids: str) -> list[dict[str, str]]:
    return [{"operation_id": op_id} for op_id in op_ids]


def _machine(machine_id: str, **fields: Any) -> Machine:
    return Machine.model_validate({"id": machine_id, "work_center_id": WC} | fields)


def _oven(machine_id: str = "oven") -> Machine:
    return _machine(machine_id, batch=OVEN_CAPACITY)


def _job(job_id: str, *operations: Operation, **fields: Any) -> Job:
    return Job.model_validate({"id": job_id, "operations": operations} | fields)


def _jobs(*operations: Operation) -> list[Job]:
    """One job per operation, named after it."""
    return [_job(op.id, op) for op in operations]


def _problem(jobs: Sequence[Job], machines: Sequence[Machine], **fields: Any) -> Problem:
    data: dict[str, Any] = {
        "id": "absorbed",
        "jobs": tuple(jobs),
        "machines": tuple(machines),
        "work_centers": (WorkCenter(id=WC),),
    }
    return Problem.model_validate(data | fields)


def _request(problem: Problem, **fields: Any) -> SolveRequest:
    return SolveRequest.model_validate({"schema_version": "2.0", "problem": problem} | fields)


def _solved(request: SolveRequest) -> SolveResult:
    """Baseline result that must have verified; a failure shows the issues."""
    result = solve(request, solver=BaselineSolver())
    assert result.verified, result.issues
    return result


def _by_op(schedule: Schedule) -> dict[str, Assignment]:
    return {a.operation_id: a for a in schedule.assignments}


def _codes(request: SolveRequest, schedule: Schedule) -> set[str]:
    verification = verify(
        apply_overrides(request), request.overrides, schedule, request.previous_plan
    )
    assert verification.valid is (not verification.issues)
    return {issue.code for issue in verification.issues}


def _lexicographic(*kinds: ObjectiveKind) -> Lexicographic:
    return Lexicographic(levels=tuple(LexicographicLevel(kind=kind) for kind in kinds))


# --- capacity and windows ------------------------------------------------------------------


def test_capacity_two_is_two_unary_machines_running_at_once() -> None:
    """Absorbed from tests/production/test_schedule_integrity.py::
    test_capacity_two_allows_two_simultaneous_occupations.

    Adapted: a capacity-2 resource is two unary machines in one work center (D-062); each
    operation may run on either.
    """
    options = (("M1", 10.0, 0.0), ("M2", 10.0, 0.0))
    problem = _problem(
        _jobs(_op("a", *options), _op("b", *options)), [_machine("M1"), _machine("M2")]
    )

    result = _solved(_request(problem))

    assert [a.start for a in result.schedule.assignments] == [0.0, 0.0]
    assert {a.machine_id for a in result.schedule.assignments} == {"M1", "M2"}


def test_machine_window_too_short_falls_back_to_the_other_alternative() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_machine_specific_duration_hard_eligibility_preference_and_fallback (fallback part).

    Adapted: the production `Resource.windows` (0, 30) is a machine calendar (D-065). The
    40 s option on 441 cannot fit the 30 s window under `forbid`, so the 60 s option on
    343 is used.
    """
    op = _op("machine-specific", ("343", 60.0, 10.0), ("441", 40.0, 0.0))
    problem = _problem(
        _jobs(op),
        [_machine("343"), _machine("441", calendar_id="short")],
        calendars=({"id": "short", "intervals": ((0.0, 30.0),)},),
    )

    result = _solved(_request(problem))

    only = result.schedule.assignments[0]
    assert only.machine_id == "343"
    assert only.end - only.start == 60.0


# --- alternatives with preference cost -----------------------------------------------------


def test_machine_specific_duration_and_hard_eligibility() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_machine_specific_duration_hard_eligibility_preference_and_fallback (eligibility part).

    Each option carries its own duration; an operation with one option runs only there.
    """
    machine_specific = _op("machine-specific", ("343", 60.0, 10.0), ("441", 40.0, 0.0))
    restricted = _op("restricted", ("529", 20.0, 0.0))
    problem = _problem(
        _jobs(machine_specific, restricted), [_machine("343"), _machine("441"), _machine("529")]
    )

    by_op = _by_op(_solved(_request(problem)).schedule)

    assert by_op["machine-specific"].machine_id == "441"
    assert by_op["machine-specific"].end - by_op["machine-specific"].start == 40.0
    assert by_op["restricted"].machine_id == "529"


def test_preference_cost_breaks_an_equal_duration_tie_through_soft_penalty() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_machine_specific_duration_hard_eligibility_preference_and_fallback (preference part).

    Adapted: v2 solvers choose a machine by completion time; `preference_cost` is scored
    by the `soft_penalty` objective (D-068). Two plans, one per alternative (pinned), are
    evaluated and ranked by makespan then soft penalty: the equal-duration plan on the
    cheaper 441 wins.
    """
    machines = [_machine("343"), _machine("441")]
    options = (("343", 50.0, 5.0), ("441", 50.0, 0.0))
    values = {}
    for machine_id in ("343", "441"):
        op = _op("preference-only", *options, pin={"machine_id": machine_id})
        request = _request(_problem(_jobs(op), machines))
        result = _solved(request)
        values[machine_id] = evaluate(apply_overrides(request), result.schedule)

    assert values["343"][ObjectiveKind.SOFT_PENALTY] == 5.0
    assert values["441"][ObjectiveKind.SOFT_PENALTY] == 0.0
    spec = _lexicographic(ObjectiveKind.MAKESPAN, ObjectiveKind.SOFT_PENALTY)
    assert rank(values, spec) == ["441", "343"]


# --- multi-resource (operator) phases ------------------------------------------------------


def test_shared_resource_held_in_one_phase_serializes_that_phase_only() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_pipeline_tail_occupies_shared_oven_without_multiplying_dwell.

    Adapted: the shared oven used during a short phase is a pool of size 1 held only
    during the setup phase (D-063; v2 phases are setup then run, so the short phase comes
    first). Each operation lasts setup + run (dwell not multiplied) and the two holds of
    the shared resource do not overlap while the runs do.
    """
    tail = SetupFunction(id="tail", terms=(), default_seconds=5.0)
    need = {"pool_id": "oven", "quantity": 1, "phase": "setup"}
    problem = _problem(
        _jobs(
            _op("WO-1:coil", ("coiler-a", 60.0, 0.0), operator_needs=[need]),
            _op("WO-2:coil", ("coiler-b", 120.0, 0.0), operator_needs=[need]),
        ),
        [
            _machine("coiler-a", setup_function_id="tail"),
            _machine("coiler-b", setup_function_id="tail"),
        ],
        operator_pools=({"id": "oven", "size": 1},),
        setup_functions=(tail,),
    )

    by_op = _by_op(_solved(_request(problem)).schedule)

    first, second = by_op["WO-1:coil"], by_op["WO-2:coil"]
    assert first.end - first.start == 65.0
    assert second.end - second.start == 125.0
    holds = sorted((a.start, a.setup_end) for a in (first, second))
    assert holds[0][1] <= holds[1][0]
    assert second.start < first.end


def test_verifier_rejects_forged_overlap_on_a_shared_run_phase_resource() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_verifier_rejects_forged_auxiliary_overlap_and_missing_occupation (overlap part).

    Adapted: the robot is a pool of size 1 held during the run phase (D-063). Moving the
    second operation onto the first one's times on its own machine overloads the pool.
    The missing-occupation part has no v2 form: pool use is derived from the assignment.
    """
    need = {"pool_id": "robot", "quantity": 1, "phase": "run"}
    problem = _problem(
        _jobs(
            _op("op-1", ("m1", 30.0, 0.0), operator_needs=[need]),
            _op("op-2", ("m2", 30.0, 0.0), operator_needs=[need]),
        ),
        [_machine("m1"), _machine("m2")],
        operator_pools=({"id": "robot", "size": 1},),
    )
    request = _request(problem)
    schedule = _solved(request).schedule
    first, second = (assignment_of(schedule, op_id) for op_id in ("op-1", "op-2"))
    assert first.end <= second.start

    forged = shift(schedule, "op-2", first.start - second.start)

    assert "operator_capacity" in _codes(request, forged)


# --- setup phase ---------------------------------------------------------------------------


def test_full_setup_is_charged_for_each_same_family_job() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_full_setup_is_charged_for_each_same_family_job_and_revision_is_retained.

    Adapted: the setup `Phase` is the setup phase from a `SetupFunction` (D-063, D-064).
    Equal families get no implicit 0 (no diagonal matrix entry), so both jobs pay the
    full default 10 s. Recipe revision has no v2 field and is not ported.
    """
    full = SetupFunction(id="full", terms=(), default_seconds=10.0)
    problem = _problem(
        _jobs(
            _op("same-wire-1", ("441", 20.0, 0.0), setup_family="wire"),
            _op("same-wire-2", ("441", 20.0, 0.0), setup_family="wire"),
        ),
        [_machine("441", setup_function_id="full")],
        setup_functions=(full,),
    )

    result = _solved(_request(problem))

    for a in result.schedule.assignments:
        assert a.setup_end - a.start == 10.0
        assert a.end - a.setup_end == 20.0
    assert result.objective_values[ObjectiveKind.TOTAL_SETUP] == 20.0


# --- batching ------------------------------------------------------------------------------


def test_compatible_operations_share_one_batch_on_the_oven() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_compatible_batches_share_one_hold_while_capacity_and_recipe_split (shared part).

    Adapted: `ThermalRecipe` is the oven's batch capacity (100 lb) plus a batch family;
    one batch is the members sharing an `Assignment.batch_id` and its times.
    """
    problem = _problem(
        _jobs(_heat("h1", 40.0, "stress-600"), _heat("h2", 40.0, "stress-600")), [_oven()]
    )

    schedule = _solved(_request(problem)).schedule

    h1, h2 = (assignment_of(schedule, op_id) for op_id in ("h1", "h2"))
    assert h1.batch_id is not None
    assert h1.batch_id == h2.batch_id
    assert (h1.start, h1.end) == (h2.start, h2.end)
    assert h1.end - h1.start == HEAT_SECONDS


def test_batches_split_on_capacity_and_on_family() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_compatible_batches_share_one_hold_while_capacity_and_recipe_split (split part).

    70 + 40 lb exceed the 100 lb oven, and a different family never joins: three batches,
    each a full heat, verified.
    """
    problem = _problem(
        _jobs(
            _heat("h70", 70.0, "stress-600"),
            _heat("h40", 40.0, "stress-600"),
            _heat("h-other", 20.0, "stress-900"),
        ),
        [_oven()],
    )

    schedule = _solved(_request(problem)).schedule

    assert len({a.batch_id for a in schedule.assignments}) == 3
    assert all(a.end - a.start == HEAT_SECONDS for a in schedule.assignments)


def test_verifier_rejects_forged_batch_over_capacity_or_mixing_families() -> None:
    """Absorbed from tests/production/test_scheduling.py::
    test_verifier_rejects_forged_batch_over_capacity_and_incompatible_members.

    Adapted: v2 reports both faults with the one `batch` code (the production codes were
    `batch_capacity` and `batch_compatibility`); the message names the fault.
    """
    for other, quantity, family, fault in (
        ("h40", 40.0, "stress", "exceeds capacity"),
        ("h-other", 20.0, "other", "mix batch families"),
    ):
        heats = _jobs(_heat("h70", 70.0, "stress"), _heat(other, quantity, family))
        request = _request(_problem(heats, [_oven()]))
        schedule = _solved(request).schedule
        assert assignment_of(schedule, "h70").batch_id != assignment_of(schedule, other).batch_id

        forged = join_batch(schedule, other, "h70")

        verification = verify(apply_overrides(request), (), forged)
        assert [issue.code for issue in verification.issues] == ["batch"], verification.issues
        assert fault in verification.issues[0].message


def test_batch_obeys_predecessors_and_a_repeated_oven_visit() -> None:
    """Absorbed from tests/production/test_schedule_integrity.py::
    test_batch_obeys_predecessor_and_repeated_resource_visit.

    One job visits the oven twice; each heat waits for its predecessor and the two visits
    are separate batches that do not overlap.
    """
    job = _job(
        "J",
        _op("coil", ("machine", 10.0, 0.0)),
        _heat("heat", 40.0, "heat", predecessors=_after("coil")),
        _op("finish", ("machine", 5.0, 0.0), predecessors=_after("heat")),
        _heat("heat-2", 40.0, "heat", predecessors=_after("finish")),
    )
    problem = _problem([job], [_machine("machine"), _oven()])

    by_op = _by_op(_solved(_request(problem)).schedule)

    assert by_op["coil"].end <= by_op["heat"].start
    assert by_op["heat"].end <= by_op["finish"].start
    assert by_op["finish"].end <= by_op["heat-2"].start
    assert by_op["heat"].batch_id != by_op["heat-2"].batch_id


def test_batch_members_on_different_ovens_are_not_grouped() -> None:
    """Absorbed from tests/production/test_schedule_integrity.py::
    test_batch_members_on_different_resources_are_not_grouped.
    """
    problem = _problem(
        _jobs(_heat("a", 40.0, "heat", oven="oven-a"), _heat("b", 40.0, "heat", oven="oven-b")),
        [_oven("oven-a"), _oven("oven-b")],
    )

    schedule = _solved(_request(problem)).schedule

    assert {(a.machine_id, a.batch_id) for a in schedule.assignments} == {
        ("oven-a", "oven-a#b1"),
        ("oven-b", "oven-b#b1"),
    }


def test_verifier_requires_full_batch_duration_and_every_member() -> None:
    """Absorbed from tests/production/test_schedule_integrity.py::
    test_verifier_requires_batch_record_duration_members_and_occupation.

    Adapted: v2 has no separate batch record (dropping it has no v2 form). A shortened
    member breaks its run length and the shared batch times; a dropped member is a
    missing assignment.
    """
    problem = _problem(_jobs(_heat("a", 40.0, "heat"), _heat("b", 40.0, "heat")), [_oven()])
    request = _request(problem)
    schedule = _solved(request).schedule

    assert {"duration", "batch"} <= _codes(request, shorten_run(schedule, "a", 69.0))
    assert _codes(request, drop(schedule, "a")) == {"missing_assignment"}


# --- verifier integrity ----------------------------------------------------------------------


def test_verifier_rejects_duplicate_wrong_length_and_early_assignments() -> None:
    """Absorbed from tests/production/test_schedule_integrity.py::
    test_verifier_rejects_duplicate_shape_release_split_and_gapped_coverage.

    Adapted: production `release_time` is `Operation.ready_time`; a duplicate is reported
    as `unknown_assignment` ("assigned twice"). The split-segment and gapped-occupation
    parts have no v2 form: an assignment is one `[start, setup_end, end)` record.
    """
    problem = _problem(
        _jobs(_op("op", ("machine", 10.0, 0.0), ready_time=50.0)), [_machine("machine")]
    )
    request = _request(problem)
    schedule = _solved(request).schedule
    only = schedule.assignments[0]
    assert only.start == 50.0

    duplicate = schedule.model_copy(update={"assignments": (only, only)})
    assert "unknown_assignment" in _codes(request, duplicate)
    assert _codes(request, replace(schedule, "op", end=only.end + 1)) == {"duration"}
    assert _codes(request, shift(schedule, "op", -50.0)) == {"readiness"}


# --- objectives (service metrics and promises) -----------------------------------------------


def _promise_request() -> SolveRequest:
    """Three jobs in a chain on one machine: on time, late (weight class 2), no due date."""
    jobs = [
        _job("sales-on-time", _op("op-on-time", ("machine", 10.0, 0.0)), due=20.0),
        _job(
            "sales-late",
            _op("op-late", ("machine", 30.0, 0.0), predecessors=_after("op-on-time")),
            due=25.0,
            weight_class=2,
            sales_order_refs=("SO-2",),
        ),
        _job(
            "replenishment", _op("op-stock", ("machine", 5.0, 0.0), predecessors=_after("op-late"))
        ),
    ]
    profile = {"objective": _lexicographic(ObjectiveKind.WEIGHTED_TARDINESS)}
    return _request(_problem(jobs, [_machine("machine")]), profile=profile)


def test_due_dates_drive_tardiness_and_jobs_without_due_are_out_of_scope() -> None:
    """Absorbed from tests/production/test_commitments.py::
    test_individual_promises_tardiness_and_replenishment_service_scope.

    Adapted: per-job results are objective values (D-068); a job without a due date
    (replenishment) is left out of every due-date term but still counts in makespan.
    Cohort and the customer on-time fraction have no v2 field and are not ported.
    """
    result = _solved(_promise_request())

    assert [a.end for a in result.schedule.assignments] == [10.0, 40.0, 45.0]
    values = result.objective_values
    assert values[ObjectiveKind.TOTAL_TARDINESS] == 15.0
    assert values[ObjectiveKind.WEIGHTED_TARDINESS] == 30.0
    assert values[ObjectiveKind.TARDY_COUNT] == 1.0
    assert values[ObjectiveKind.MAKESPAN] == 45.0
    assert result.schedule.primary_value == 30.0


def test_verifier_rejects_a_forged_objective_value() -> None:
    """Absorbed from tests/production/test_commitments.py::
    test_verifier_rejects_forged_job_tardiness
    and tests/production/test_schedule_integrity.py::
    test_verifier_recomputes_service_metrics_and_status_consistency (metric part).

    Adapted: v2 schedules report one primary value, recomputed by the verifier; a forged
    value is `objective_mismatch`. The status part has no v2 form: `solve` verifies only
    FEASIBLE or OPTIMAL answers.
    """
    request = _promise_request()
    schedule = _solved(request).schedule

    assert _codes(request, with_primary(schedule, 0.0)) == {"objective_mismatch"}


def test_job_rejects_fields_it_does_not_declare() -> None:
    """Absorbed from tests/production/test_commitments.py::
    test_invalid_cohort_and_unknown_demand_kind_are_rejected (no-inference part).

    Adapted: v2 has no `demand_kind` or `erp_end_date`; a job carrying either is refused at
    the boundary instead of being inferred from. The cohort part is not ported (no cohort).
    """
    base = {"id": "no-inference", "operations": ()}
    for extra in ({"demand_kind": "sales-ish"}, {"erp_end_date": 100.0}):
        with pytest.raises(ValidationError):
            Job.model_validate(base | extra)
    assert validate(_promise_request()) == []
