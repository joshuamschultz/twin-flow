"""Acceptance tests for per-order promises and cohort scope (TF-WS-010)."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module

import pytest


def _api():
    try:
        contracts = import_module("twinflow.production.contracts")
        scheduling = import_module("twinflow.production.scheduling")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production commitments: {error}", pytrace=False)
    return contracts, scheduling


def _operation(api, operation_id: str, order_id: str, duration: float, predecessors=()):
    return api.ProductionOperation(
        id=operation_id,
        order_id=order_id,
        predecessors=predecessors,
        alternatives=(
            api.RecipeAlternative(
                id=f"{operation_id}-recipe",
                primary_resource_id="machine",
                revision="r1",
                phases=(api.Phase("run", duration),),
                uses=(api.ResourceUse(("machine",), "run", "run"),),
            ),
        ),
    )


def _problem(api, cohort_id: str = "snapshot-a"):
    cohort = api.ScenarioCohort(
        id=cohort_id,
        snapshot_at="2026-08-07T11:46:00-06:00",
        included_job_ids=("sales-on-time", "sales-late", "replenishment"),
        total_work_order_count=126,
        pending_demand_count=None,
    )
    return api.ProductionProblem(
        resources=(
            api.Resource(
                "machine",
                "machine",
                windows=(api.TimeWindow(0, 200),),
            ),
        ),
        operations=(
            _operation(api, "op-on-time", "sales-on-time", 10),
            _operation(api, "op-late", "sales-late", 30, ("op-on-time",)),
            _operation(api, "op-stock", "replenishment", 5, ("op-late",)),
        ),
        jobs=(
            api.ProductionJob(
                "sales-on-time",
                ("op-on-time",),
                demand_kind="customer",
                promised_ship_time=20,
                priority=1,
                sales_order_refs=("SO-1",),
            ),
            api.ProductionJob(
                "sales-late",
                ("op-late",),
                demand_kind="customer",
                promised_ship_time=25,
                priority=2,
                sales_order_refs=("SO-2",),
            ),
            api.ProductionJob(
                "replenishment",
                ("op-stock",),
                demand_kind="replenishment",
            ),
        ),
        objective="weighted_tardiness",
        cohort=cohort,
    )


def test_individual_promises_tardiness_and_replenishment_service_scope() -> None:
    api, scheduling = _api()
    problem = _problem(api)
    schedule = scheduling.solve(problem, solver="baseline")
    jobs = {item.job_id: item for item in schedule.job_results}

    assert jobs["sales-on-time"].completion_time == 10
    assert jobs["sales-on-time"].tardiness == 0
    assert jobs["sales-on-time"].on_time is True
    assert jobs["sales-late"].completion_time == 40
    assert jobs["sales-late"].tardiness == 15
    assert jobs["sales-late"].on_time is False
    assert jobs["replenishment"].completion_time == 45
    assert jobs["replenishment"].tardiness is None
    assert jobs["replenishment"].on_time is None
    assert jobs["replenishment"].customer_demand is False
    assert schedule.customer_on_time_fraction == 0.5
    assert schedule.objective_value == 30
    assert schedule.cohort.total_work_order_count == 126
    assert schedule.cohort.pending_demand_count is None


def test_verifier_rejects_forged_job_tardiness() -> None:
    api, scheduling = _api()
    problem = _problem(api)
    schedule = scheduling.solve(problem, solver="baseline")
    forged_results = tuple(
        replace(result, tardiness=0, on_time=True) if result.job_id == "sales-late" else result
        for result in schedule.job_results
    )
    forged = replace(schedule, job_results=forged_results, verified=True)

    verification = scheduling.verify(problem, forged)

    assert verification.valid is False
    assert any(issue.code == "job_tardiness" for issue in verification.issues)


def test_different_cohorts_are_explicitly_noncomparable() -> None:
    api, scheduling = _api()
    left = scheduling.solve(_problem(api, "snapshot-a"), solver="baseline")
    right = scheduling.solve(_problem(api, "snapshot-b"), solver="baseline")
    comparison = scheduling.compare_schedules(left, right)
    assert comparison.comparable is False
    assert any(issue.code == "cohort_mismatch" for issue in comparison.issues)


def test_invalid_cohort_and_unknown_demand_kind_are_rejected() -> None:
    api, scheduling = _api()
    invalid_cohort = replace(
        _problem(api),
        cohort=api.ScenarioCohort(
            id="bad",
            snapshot_at="2026-08-07T11:46:00-06:00",
            included_job_ids=("unknown",),
            total_work_order_count=0,
        ),
    )
    assert any(issue.code == "cohort" for issue in scheduling.validate(invalid_cohort))

    problem = _problem(api)
    bad_job = replace(problem.jobs[0], demand_kind="sales-ish")
    invalid_kind = replace(problem, jobs=(bad_job, *problem.jobs[1:]))
    assert any(issue.code == "demand_kind" for issue in scheduling.validate(invalid_kind))

    with pytest.raises(TypeError):
        api.ProductionJob(
            "no-inference",
            ("op-on-time",),
            demand_kind="customer",
            erp_end_date=100,
        )
