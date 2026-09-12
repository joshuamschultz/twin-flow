"""Adversarial checks for production schedule composition and verification."""

from __future__ import annotations

from dataclasses import replace

from twinflow.production import contracts as api
from twinflow.production import scheduling


def _resource(resource_id: str, *, capacity: int = 1) -> api.Resource:
    return api.Resource(
        resource_id,
        "machine",
        capacity=capacity,
        windows=(api.TimeWindow(0, 500),),
    )


def _operation(
    operation_id: str,
    resource_id: str,
    duration: float,
    *,
    predecessors: tuple[str, ...] = (),
    release_time: float = 0,
) -> api.ProductionOperation:
    return api.ProductionOperation(
        operation_id,
        operation_id,
        (
            api.RecipeAlternative(
                f"{operation_id}-recipe",
                resource_id,
                "r1",
                (api.Phase("run", duration),),
                (api.ResourceUse((resource_id,), "run", "run"),),
            ),
        ),
        predecessors=predecessors,
        release_time=release_time,
    )


def _heat(
    operation_id: str,
    resource_id: str = "oven",
    *,
    predecessors: tuple[str, ...] = (),
) -> api.ProductionOperation:
    return api.ProductionOperation(
        operation_id,
        operation_id,
        (
            api.RecipeAlternative(
                f"{operation_id}-recipe",
                resource_id,
                "r1",
                (),
                (),
                batch=api.BatchRequirement(f"lot-{operation_id}", 40, "lb", "heat"),
            ),
        ),
        predecessors=predecessors,
    )


def test_capacity_two_allows_two_simultaneous_occupations() -> None:
    problem = api.ProductionProblem(
        (_resource("machine", capacity=2),),
        (_operation("a", "machine", 10), _operation("b", "machine", 10)),
    )
    schedule = scheduling.solve(problem)
    assert [assignment.start for assignment in schedule.assignments] == [0, 0]
    assert scheduling.verify(problem, schedule).valid is True


def test_batch_obeys_predecessor_and_repeated_resource_visit() -> None:
    problem = api.ProductionProblem(
        (_resource("machine"), _resource("oven")),
        (
            _operation("coil", "machine", 10),
            _heat("heat", predecessors=("coil",)),
            _operation("finish", "machine", 5, predecessors=("heat",)),
            _heat("heat-2", predecessors=("finish",)),
        ),
        thermal_recipes=(api.ThermalRecipe("heat", "r1", 100, "lb", 10, 60),),
    )
    schedule = scheduling.solve(problem)
    assignments = {item.operation_id: item for item in schedule.assignments}
    assert assignments["coil"].end <= assignments["heat"].start
    assert assignments["heat"].end <= assignments["finish"].start
    assert assignments["finish"].end <= assignments["heat-2"].start
    oven_occupations = [
        occupation
        for assignment in schedule.assignments
        for occupation in assignment.occupations
        if occupation.resource_id == "oven"
    ]
    assert len(oven_occupations) == 2
    assert oven_occupations[0].end <= oven_occupations[1].start
    assert scheduling.verify(problem, schedule).valid is True


def test_batch_members_on_different_resources_are_not_grouped() -> None:
    problem = api.ProductionProblem(
        (_resource("oven-a"), _resource("oven-b")),
        (_heat("a", "oven-a"), _heat("b", "oven-b")),
        thermal_recipes=(api.ThermalRecipe("heat", "r1", 100, "lb", 10, 60),),
    )
    schedule = scheduling.solve(problem)
    assert len(schedule.batches) == 2
    assert {batch.resource_id for batch in schedule.batches} == {"oven-a", "oven-b"}


def test_verifier_requires_batch_record_duration_members_and_occupation() -> None:
    problem = api.ProductionProblem(
        (_resource("oven"),),
        (_heat("a"), _heat("b")),
        thermal_recipes=(api.ThermalRecipe("heat", "r1", 100, "lb", 10, 60),),
    )
    schedule = scheduling.solve(problem)
    assert scheduling.verify(problem, replace(schedule, batches=())).valid is False
    short = replace(schedule.batches[0], end=1)
    assert any(
        issue.code == "batch_duration"
        for issue in scheduling.verify(problem, replace(schedule, batches=(short,))).issues
    )
    member = schedule.assignments[0]
    no_occupation = replace(member, occupations=())
    forged = replace(schedule, assignments=(no_occupation, *schedule.assignments[1:]))
    assert any(
        issue.code == "missing_resource_occupation"
        for issue in scheduling.verify(problem, forged).issues
    )


def test_verifier_rejects_duplicate_shape_release_split_and_gapped_coverage() -> None:
    operation = _operation("op", "machine", 10, release_time=50)
    problem = api.ProductionProblem((_resource("machine"),), (operation,))
    schedule = scheduling.solve(problem)
    assignment = schedule.assignments[0]

    duplicate = replace(schedule, assignments=(assignment, assignment))
    assert any(
        issue.code == "duplicate_assignment"
        for issue in scheduling.verify(problem, duplicate).issues
    )
    wrong_bounds = replace(schedule, assignments=(replace(assignment, end=assignment.end + 1),))
    assert scheduling.verify(problem, wrong_bounds).valid is False
    early = replace(
        assignment,
        start=0,
        end=10,
        segments=(api.PhaseSegment("op", "run", 0, 10),),
        occupations=(api.ResourceOccupation("op", "machine", 0, 10),),
    )
    assert any(
        issue.code == "release_time"
        for issue in scheduling.verify(problem, replace(schedule, assignments=(early,))).issues
    )
    split = replace(
        assignment,
        segments=(
            api.PhaseSegment("op", "run", 50, 55),
            api.PhaseSegment("op", "run", 60, 65),
        ),
        end=65,
        occupations=(api.ResourceOccupation("op", "machine", 50, 65),),
    )
    assert any(
        issue.code == "noninterruptible_split"
        for issue in scheduling.verify(problem, replace(schedule, assignments=(split,))).issues
    )
    gapped = replace(
        assignment,
        occupations=(
            api.ResourceOccupation("op", "machine", 50, 51),
            api.ResourceOccupation("op", "machine", 59, 60),
        ),
    )
    assert any(
        issue.code == "missing_resource_occupation"
        for issue in scheduling.verify(problem, replace(schedule, assignments=(gapped,))).issues
    )


def test_verifier_recomputes_service_metrics_and_status_consistency() -> None:
    operation = _operation("op", "machine", 10)
    problem = api.ProductionProblem(
        (_resource("machine"),),
        (operation,),
        jobs=(api.ProductionJob("op", ("op",), "customer", 20),),
    )
    schedule = scheduling.solve(problem)
    forged_metric = replace(schedule, customer_on_time_fraction=0.0)
    assert any(
        issue.code == "service_metric" for issue in scheduling.verify(problem, forged_metric).issues
    )
    forged_status = replace(schedule, status="INFEASIBLE")
    assert any(issue.code == "status" for issue in scheduling.verify(problem, forged_status).issues)
