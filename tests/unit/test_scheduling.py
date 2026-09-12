from __future__ import annotations

import pytest

from twinflow.scheduling import (
    Operation,
    ResourceWindow,
    SchedulingProblem,
    pareto_frontier,
    rank_candidates,
    solve,
    translate_legacy,
    verify_schedule,
)
from twinflow.scheduling.ortools import OptionalDependencyError, ORToolsSolver


def _problem() -> SchedulingProblem:
    return SchedulingProblem(
        operations=(
            Operation("cut", 2, required_qualifications=frozenset({"machinist"})),
            Operation("assemble", 3, ("cut",), required_qualifications=frozenset({"assembler"})),
        ),
        resources=(
            ResourceWindow("m1", 0, 20, frozenset({"machinist"})),
            ResourceWindow("b1", 0, 20, frozenset({"assembler"})),
        ),
    )


def test_hand_solved_schedule_and_independent_verifier() -> None:
    result = solve(_problem())
    assert result.status == "FEASIBLE"
    assert result.starts == {"cut": 0, "assemble": 2}
    assert result.verified
    assert verify_schedule(_problem(), result).valid


def test_infeasible_qualification_calendar_and_frozen_conflict() -> None:
    bad = SchedulingProblem(
        (Operation("x", 3, required_qualifications=frozenset({"q"})),),
        (ResourceWindow("r", 0, 2, frozenset({"q"})),),
    )
    assert solve(bad).status == "INFEASIBLE"
    frozen = SchedulingProblem(
        (
            Operation("x", 2, frozen_start=0, frozen_resource="r"),
            Operation("y", 2, frozen_start=1, frozen_resource="r"),
        ),
        (ResourceWindow("r", 0, 10),),
    )
    assert solve(frozen).status == "INFEASIBLE"


def test_translation_refuses_distribution_and_supports_rate_based() -> None:
    model = {
        "locations": [
            {
                "name": "cut",
                "machine": "m",
                "labor_skill": "machinist",
                "time_model": {"kind": "rate_based", "rate": 0.5},
            }
        ],
        "machines": [{"name": "m"}],
        "routing": [{"part": "finished", "steps": ["cut"]}],
    }
    problem = translate_legacy(
        model, [{"work_order_id": "wo", "part": "finished", "start_date": 0}]
    )
    assert solve(problem).verified
    model["locations"][0]["time_model"] = {"kind": "distribution", "name": "normal"}
    with pytest.raises(ValueError, match="unsupported"):
        translate_legacy(model, [{"work_order_id": "wo", "part": "finished"}])


def test_ranking_and_optional_solver_status() -> None:
    candidate = solve(_problem())
    assert rank_candidates([candidate]) == [candidate]
    assert pareto_frontier([candidate]) == [candidate]
    try:
        optional = ORToolsSolver()
    except OptionalDependencyError:
        return
    assert optional.solve(_problem()).status == "UNKNOWN"
