"""Acceptance tests for rich production scheduling (TF-WS-001/003/006)."""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module

import pytest


def _api():
    try:
        contracts = import_module("twinflow.production.contracts")
        scheduling = import_module("twinflow.production.scheduling")
    except (ImportError, ModuleNotFoundError) as error:
        pytest.fail(f"MISSING FEATURE twinflow.production scheduling API: {error}", pytrace=False)
    required_contracts = (
        "TimeWindow",
        "Resource",
        "Phase",
        "ResourceUse",
        "RestartRule",
        "ThermalRecipe",
        "BatchRequirement",
        "RecipeAlternative",
        "ProductionOperation",
        "ProductionProblem",
        "ProductionSchedule",
    )
    missing = [name for name in required_contracts if not hasattr(contracts, name)]
    if missing:
        pytest.fail(f"MISSING FEATURE production contracts: {', '.join(missing)}", pytrace=False)
    for name in ("validate", "solve", "verify"):
        if not hasattr(scheduling, name):
            pytest.fail(f"MISSING FEATURE production scheduling.{name}", pytrace=False)
    return contracts, scheduling


def _resource(api, resource_id: str, kind: str = "machine", end: float = 500):
    return api.Resource(
        id=resource_id,
        kind=kind,
        capacity=1,
        windows=(api.TimeWindow(0, end),),
    )


def _alternative(
    api,
    alternative_id: str,
    resource_id: str,
    duration: float,
    *,
    revision: str = "rev-1",
    preference_cost: float = 0,
):
    return api.RecipeAlternative(
        id=alternative_id,
        primary_resource_id=resource_id,
        revision=revision,
        phases=(api.Phase("run", duration),),
        uses=(api.ResourceUse((resource_id,), "run", "run"),),
        preference_cost=preference_cost,
    )


def test_pipeline_tail_occupies_shared_oven_without_multiplying_dwell() -> None:
    api, scheduling = _api()
    resources = (
        _resource(api, "coiler-a"),
        _resource(api, "coiler-b"),
        _resource(api, "oven", "oven"),
    )

    def pipeline(operation_id: str, order_id: str, coiler: str, run: float):
        return api.ProductionOperation(
            id=operation_id,
            order_id=order_id,
            alternatives=(
                api.RecipeAlternative(
                    id=f"{coiler}-pipeline",
                    primary_resource_id=coiler,
                    revision="setup-sheet-1",
                    phases=(api.Phase("production", run), api.Phase("tail", 5)),
                    uses=(
                        api.ResourceUse((coiler,), "production", "production"),
                        api.ResourceUse(("oven",), "production", "tail"),
                    ),
                ),
            ),
        )

    problem = api.ProductionProblem(
        resources=resources,
        operations=(
            pipeline("WO-1:coil", "WO-1", "coiler-a", 60),
            pipeline("WO-2:coil", "WO-2", "coiler-b", 120),
        ),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    verification = scheduling.verify(problem, schedule)

    assert schedule.status == "FEASIBLE"
    assert schedule.verified is True
    assert verification.valid is True
    assignments = {item.operation_id: item for item in schedule.assignments}
    assert assignments["WO-1:coil"].end - assignments["WO-1:coil"].start == 65
    assert assignments["WO-2:coil"].end - assignments["WO-2:coil"].start == 125
    oven = sorted(
        (
            occupation
            for assignment in schedule.assignments
            for occupation in assignment.occupations
            if occupation.resource_id == "oven"
        ),
        key=lambda item: item.start,
    )
    assert oven[0].end <= oven[1].start


def test_verifier_rejects_forged_auxiliary_overlap_and_missing_occupation() -> None:
    api, scheduling = _api()
    resources = (_resource(api, "m1"), _resource(api, "m2"), _resource(api, "robot"))

    def robotic(operation_id: str, machine_id: str):
        return api.ProductionOperation(
            id=operation_id,
            order_id=operation_id,
            alternatives=(
                api.RecipeAlternative(
                    id=machine_id,
                    primary_resource_id=machine_id,
                    revision="r1",
                    phases=(api.Phase("run", 30),),
                    uses=(
                        api.ResourceUse((machine_id,), "run", "run"),
                        api.ResourceUse(("robot",), "run", "run"),
                    ),
                ),
            ),
        )

    problem = api.ProductionProblem(
        resources=resources,
        operations=(robotic("op-1", "m1"), robotic("op-2", "m2")),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    first, second = schedule.assignments
    first_robot = next(item for item in first.occupations if item.resource_id == "robot")
    forged_occupations = tuple(
        replace(item, start=first_robot.start, end=first_robot.end)
        if item.resource_id == "robot"
        else item
        for item in second.occupations
    )
    forged = replace(
        schedule,
        assignments=(first, replace(second, occupations=forged_occupations)),
        verified=True,
    )

    verification = scheduling.verify(problem, forged)

    assert verification.valid is False
    assert any(issue.code == "resource_overlap" for issue in verification.issues)

    missing_robot = replace(
        schedule,
        assignments=(
            first,
            replace(
                second,
                occupations=tuple(
                    item for item in second.occupations if item.resource_id != "robot"
                ),
            ),
        ),
        verified=True,
    )
    missing_verification = scheduling.verify(problem, missing_robot)
    assert missing_verification.valid is False
    assert any(issue.code == "missing_resource_occupation" for issue in missing_verification.issues)


def test_machine_specific_duration_hard_eligibility_preference_and_fallback() -> None:
    api, scheduling = _api()
    resources = (
        _resource(api, "343"),
        _resource(api, "441"),
        _resource(api, "529"),
    )
    machine_specific = api.ProductionOperation(
        id="machine-specific",
        order_id="WO-1",
        alternatives=(
            _alternative(api, "on-343", "343", 60, preference_cost=10),
            _alternative(api, "on-441", "441", 40, preference_cost=0),
        ),
    )
    restricted = api.ProductionOperation(
        id="restricted",
        order_id="WO-2",
        alternatives=(_alternative(api, "only-529", "529", 20),),
    )
    preference_only = api.ProductionOperation(
        id="preference-only",
        order_id="WO-3",
        alternatives=(
            _alternative(api, "equal-343", "343", 50, preference_cost=5),
            _alternative(api, "equal-441", "441", 50, preference_cost=0),
        ),
    )
    problem = api.ProductionProblem(
        resources=resources,
        operations=(machine_specific, restricted, preference_only),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    assignments = {item.operation_id: item for item in schedule.assignments}

    assert assignments["machine-specific"].alternative_id == "on-441"
    assert assignments["machine-specific"].end - assignments["machine-specific"].start == 40
    assert {item.resource_id for item in assignments["restricted"].occupations} == {"529"}
    assert assignments["preference-only"].alternative_id == "equal-441"

    fallback_problem = replace(
        problem,
        resources=(
            _resource(api, "343"),
            api.Resource("441", "machine", windows=(api.TimeWindow(0, 30),)),
            _resource(api, "529"),
        ),
        operations=(machine_specific,),
    )
    fallback = scheduling.solve(fallback_problem, solver="baseline")
    assert fallback.assignments[0].alternative_id == "on-343"


def test_full_setup_is_charged_for_each_same_family_job_and_revision_is_retained() -> None:
    api, scheduling = _api()

    def operation(operation_id: str, revision: str):
        alternative = api.RecipeAlternative(
            id=f"recipe-{revision}",
            primary_resource_id="441",
            revision=revision,
            phases=(api.Phase("setup", 10), api.Phase("run", 20)),
            uses=(api.ResourceUse(("441",), "setup", "run"),),
        )
        return api.ProductionOperation(
            id=operation_id,
            order_id=operation_id,
            alternatives=(alternative,),
        )

    problem = api.ProductionProblem(
        resources=(_resource(api, "441"),),
        operations=(operation("same-wire-1", "rev-1"), operation("same-wire-2", "rev-2")),
    )
    schedule = scheduling.solve(problem, solver="baseline")

    assert [assignment.recipe_revision for assignment in schedule.assignments] == ["rev-1", "rev-2"]
    for assignment in schedule.assignments:
        setup = [segment for segment in assignment.segments if segment.phase_id == "setup"]
        assert len(setup) == 1
        assert setup[0].end - setup[0].start == 10


def test_compatible_batches_share_one_hold_while_capacity_and_recipe_split() -> None:
    api, scheduling = _api()
    resources = (_resource(api, "oven", "oven", end=1000),)
    recipe_a = api.ThermalRecipe("stress-600", "rev-1", 100, "lb", 10, 60)
    recipe_b = api.ThermalRecipe(
        "stress-900",
        "rev-1",
        100,
        "lb",
        10,
        60,
        compatibility_key="900F-air",
    )

    def heat(operation_id: str, order_id: str, qty: float, recipe_id: str):
        return api.ProductionOperation(
            id=operation_id,
            order_id=order_id,
            alternatives=(
                api.RecipeAlternative(
                    id=f"{recipe_id}-on-oven",
                    primary_resource_id="oven",
                    revision="recipe-sheet-1",
                    phases=(),
                    uses=(),
                    batch=api.BatchRequirement(
                        lot_id=f"lot-{operation_id}",
                        quantity=qty,
                        uom="lb",
                        thermal_recipe_id=recipe_id,
                    ),
                ),
            ),
        )

    compatible = api.ProductionProblem(
        resources=resources,
        operations=(heat("h1", "WO-1", 40, "stress-600"), heat("h2", "WO-2", 40, "stress-600")),
        thermal_recipes=(recipe_a,),
    )
    shared = scheduling.solve(compatible, solver="baseline")
    assert len(shared.batches) == 1
    assert shared.batches[0].total_quantity == 80
    assert shared.batches[0].end - shared.batches[0].start == 70
    assert set(shared.batches[0].member_operation_ids) == {"h1", "h2"}

    split_problem = api.ProductionProblem(
        resources=resources,
        operations=(
            heat("h70", "WO-3", 70, "stress-600"),
            heat("h40", "WO-4", 40, "stress-600"),
            heat("h-other", "WO-5", 20, "stress-900"),
        ),
        thermal_recipes=(recipe_a, recipe_b),
    )
    split = scheduling.solve(split_problem, solver="baseline")
    assert len(split.batches) == 3
    assert all(batch.end - batch.start >= 70 for batch in split.batches)
    assert scheduling.verify(split_problem, split).valid is True


def test_verifier_rejects_forged_batch_over_capacity_and_incompatible_members() -> None:
    api, scheduling = _api()
    recipe = api.ThermalRecipe("stress", "r1", 100, "lb", 0, 60)

    def heat(operation_id: str, qty: float):
        return api.ProductionOperation(
            id=operation_id,
            order_id=operation_id,
            alternatives=(
                api.RecipeAlternative(
                    id="heat",
                    primary_resource_id="oven",
                    revision="r1",
                    phases=(),
                    uses=(),
                    batch=api.BatchRequirement(operation_id, qty, "lb", "stress"),
                ),
            ),
        )

    problem = api.ProductionProblem(
        resources=(_resource(api, "oven", "oven"),),
        operations=(heat("h70", 70), heat("h40", 40)),
        thermal_recipes=(recipe,),
    )
    schedule = scheduling.solve(problem, solver="baseline")
    forged_batch = replace(
        schedule.batches[0],
        member_operation_ids=("h70", "h40"),
        total_quantity=110,
    )
    forged = replace(schedule, batches=(forged_batch,), verified=True)
    verification = scheduling.verify(problem, forged)
    assert verification.valid is False
    assert any(issue.code == "batch_capacity" for issue in verification.issues)

    other_recipe = api.ThermalRecipe(
        "stress-other",
        "r1",
        100,
        "lb",
        0,
        60,
        compatibility_key="different-temperature",
    )
    incompatible_operation = api.ProductionOperation(
        id="h-other",
        order_id="h-other",
        alternatives=(
            api.RecipeAlternative(
                id="other-heat",
                primary_resource_id="oven",
                revision="r1",
                phases=(),
                uses=(),
                batch=api.BatchRequirement("h-other", 20, "lb", "stress-other"),
            ),
        ),
    )
    incompatible_problem = replace(
        problem,
        operations=(problem.operations[0], incompatible_operation),
        thermal_recipes=(recipe, other_recipe),
    )
    separate = scheduling.solve(incompatible_problem, solver="baseline")
    forged_incompatible_batch = replace(
        separate.batches[0],
        member_operation_ids=("h70", "h-other"),
        total_quantity=90,
    )
    forged_incompatible = replace(
        separate,
        batches=(forged_incompatible_batch,),
        verified=True,
    )
    incompatible_verification = scheduling.verify(incompatible_problem, forged_incompatible)
    assert incompatible_verification.valid is False
    assert any(issue.code == "batch_compatibility" for issue in incompatible_verification.issues)
