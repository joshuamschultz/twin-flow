from __future__ import annotations

import pytest

from twinflow.scheduling import (
    Operation,
    ResourceWindow,
    SchedulingProblem,
    pareto_frontier,
    problem_from_dict,
    rank_candidates,
    solve,
    translate_legacy,
    verify_schedule,
)
from twinflow.scheduling.core import ScheduleResult
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
    assert solve(bad).status == "UNKNOWN"
    frozen = SchedulingProblem(
        (
            Operation("x", 2, frozen_start=0, frozen_resource="r"),
            Operation("y", 2, frozen_start=1, frozen_resource="r"),
        ),
        (ResourceWindow("r", 0, 10),),
    )
    assert solve(frozen).status == "UNKNOWN"


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
    solved = optional.solve(_problem())
    assert solved.status == "OPTIMAL"
    assert solved.verified


def test_real_cpsat_statuses_objective_precision_and_frozen_choice() -> None:
    solver = ORToolsSolver(workers=1, seed=7)
    solved = solver.solve(_problem(), time_limit_seconds=5)
    assert solved.status == "OPTIMAL"
    assert solved.objective_value == 5
    assert solved.best_bound == 5
    impossible = SchedulingProblem((Operation("x", 2),), (ResourceWindow("r", 0, 1),))
    assert solver.solve(impossible).status == "INFEASIBLE"
    fractional = SchedulingProblem(
        (Operation("x", 0.0015, frozen_start=0, frozen_resource="r"),),
        (ResourceWindow("r", 0, 1),),
    )
    fraction_result = solver.solve(fractional)
    assert fraction_result.status == "OPTIMAL"
    assert fraction_result.verified
    assert fraction_result.ends["x"] == pytest.approx(0.0015)
    frozen_choice = SchedulingProblem(
        (Operation("x", 1, frozen_start=4, frozen_resource="r2"),),
        (ResourceWindow("r1", 0, 10), ResourceWindow("r2", 0, 10)),
    )
    frozen_result = solver.solve(frozen_choice)
    assert frozen_result.status == "OPTIMAL"
    assert frozen_result.resources == {"x": "r2"}
    lateness = SchedulingProblem(
        (Operation("x", 2, release_time=3),),
        (ResourceWindow("r", 0, 10),),
        objective="lateness",
        deadline=4,
    )
    late_result = solver.solve(lateness)
    assert late_result.status == "OPTIMAL"
    assert late_result.objective_value == 1


def test_cpsat_rejects_invalid_limit_and_reports_tiny_timeout() -> None:
    solver = ORToolsSolver()
    invalid = solve(_problem(), solver=solver, time_limit_seconds=0)
    assert invalid.status == "UNKNOWN"
    assert invalid.issues[0].code == "invalid_time_limit"
    many = SchedulingProblem(
        tuple(Operation(f"x{i}", 1) for i in range(80)),
        tuple(ResourceWindow(f"r{i}", 0, 100) for i in range(4)),
    )
    timeout = solver.solve(many, time_limit_seconds=1e-9)
    assert timeout.status == "UNKNOWN"


def test_frozen_later_slot_is_reserved_and_cascading_intervals_do_not_overlap() -> None:
    problem = SchedulingProblem(
        operations=(
            Operation("frozen", 2, frozen_start=10, frozen_resource="r"),
            Operation("a", 2),
            Operation("b", 2),
            Operation("c", 2),
        ),
        resources=(ResourceWindow("r", 0, 20),),
    )
    result = solve(problem)
    assert result.verified
    assert result.starts["frozen"] == 10
    assert result.ends["c"] <= 10


def test_numeric_validation_and_forged_solver_are_rejected() -> None:
    bad = SchedulingProblem(
        (Operation("x", 1, release_time=float("nan")),),
        (ResourceWindow("r", 0, 2),),
        deadline=float("inf"),
    )
    assert solve(bad).status == "INFEASIBLE"
    forged = ScheduleResult("FEASIBLE", {"x": 0}, {"x": 1}, {"x": "wrong"})

    class Forged:
        def solve(
            self, problem: SchedulingProblem, *, time_limit_seconds: float | None = None
        ) -> ScheduleResult:
            del problem, time_limit_seconds
            return forged

    result = solve(
        SchedulingProblem((Operation("x", 1),), (ResourceWindow("r", 0, 2),)), solver=Forged()
    )
    assert not result.verified
    assert result.issues


def test_verifier_handles_missing_ends_and_ineligible_assignment() -> None:
    problem = SchedulingProblem(
        (Operation("x", 1, eligible_resources=("r",)),), (ResourceWindow("r", 0, 2),)
    )
    forged = ScheduleResult("FEASIBLE", {"x": 0, "unknown": 0}, {}, {"x": "other"})
    check = verify_schedule(problem, forged)
    assert not check.valid
    assert any(issue.code in {"missing_assignment", "unknown_assignment"} for issue in check.issues)


def test_equal_pareto_candidates_are_both_retained() -> None:
    result = solve(_problem())
    assert len(pareto_frontier([result, result])) == 2


def test_problem_from_dict_is_strict_and_bounded() -> None:
    raw = {
        "operations": [{"id": "x", "duration": 1}],
        "resources": [{"resource_id": "r", "start": 0, "end": 2}],
    }
    assert problem_from_dict(raw).operations[0].id == "x"
    with pytest.raises(ValueError, match="unknown scheduling field"):
        problem_from_dict({**raw, "extra": True})
    with pytest.raises(ValueError, match="nonfinite"):
        problem_from_dict(
            {
                "operations": raw["operations"],
                "resources": [{"resource_id": "r", "start": 0, "end": float("inf")}],
            }
        )
