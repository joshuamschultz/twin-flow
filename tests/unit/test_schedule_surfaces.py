"""v2 ports of the still-relevant v1 scheduling cases (T-106, COMP-014, REQ-083).

Ported from `tests/unit/test_scheduling.py` and `tests/unit/test_linear_schedule.py`, which
T-107 deletes together with `scheduling/core.py`, `scheduling/ortools.py` and
`application/linear_schedule.py`. Each case keeps the v1 behavior it guarded, restated on
the v2 contract: `SolveRequest` in, `SolveResult` out through `solver.solve`.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from twinflow.application.schedule_problem import build_request
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling.model import (
    Assignment,
    ObjectiveKind,
    ObjectiveSpec,
    Problem,
    Schedule,
    SolveRequest,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, SolveContext, Solver
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import parse_request
from twinflow.scheduling.verifier import verify

# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, work_center: str, machines: list[str], seconds: float, **fields: Any) -> Any:
    options = [
        {"machine_id": m, "fixed_seconds": seconds, "unit_seconds": 0.0, "preference_cost": 0.0}
        for m in machines
    ]
    return {
        "id": op_id,
        "work_center_id": work_center,
        "quantity": 1,
        "options": options,
        **fields,
    }


def _request(
    jobs: list[dict[str, Any]],
    machines: list[dict[str, Any]],
    *,
    calendars: list[dict[str, Any]] | None = None,
    profile: dict[str, Any] | None = None,
) -> SolveRequest:
    centers = sorted({m["work_center_id"] for m in machines})
    data: dict[str, Any] = {
        "schema_version": "2.0",
        "problem": {
            "id": "p",
            "jobs": jobs,
            "machines": machines,
            "work_centers": [{"id": center} for center in centers],
            "calendars": calendars or [],
        },
    }
    if profile is not None:
        data["profile"] = profile
    return SolveRequest.model_validate(data)


def _hand_request() -> SolveRequest:
    """v1 `_problem()`: cut (2 s, machinist) then assemble (3 s, assembler).

    v1 qualifications become work centers: only m1 can cut, only b1 can assemble.
    """
    cut = _op("cut", "machining", ["m1"], 2.0)
    assemble = _op("assemble", "assembly", ["b1"], 3.0, predecessors=[{"operation_id": "cut"}])
    return _request(
        [{"id": "J", "operations": [cut, assemble]}],
        [
            {"id": "m1", "work_center_id": "machining"},
            {"id": "b1", "work_center_id": "assembly"},
        ],
    )


def _starts(schedule: Schedule) -> dict[str, float]:
    return {a.operation_id: a.start for a in schedule.assignments}


def _one_op_job(job_id: str, op: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "operations": [op], **fields}


# --- baseline and independent verifier ------------------------------------------------------


def test_baseline_hand_solved_two_step_job_is_verified_independently() -> None:
    request = _hand_request()

    result = solve(request)

    assert result.schedule.status == "FEASIBLE"
    assert _starts(result.schedule) == {"cut": 0.0, "assemble": 2.0}
    assert result.verified
    assert verify(apply_overrides(request), request.overrides, result.schedule).valid
    assert set(result.objective_values) == set(ObjectiveKind)


def test_operation_longer_than_its_calendar_window_gets_no_verified_plan() -> None:
    request = _request(
        [_one_op_job("J", _op("x", "wc", ["r"], 3.0))],
        [{"id": "r", "work_center_id": "wc", "calendar_id": "c"}],
        calendars=[{"id": "c", "intervals": [(0.0, 2.0)]}],
    )

    result = solve(request)

    assert result.schedule.status == "UNKNOWN"
    assert not result.verified
    assert [issue.code for issue in result.issues] == ["no_feasible_slot"]
    assert result.objective_values == {}


def test_conflicting_pins_on_one_machine_get_no_verified_plan() -> None:
    def pinned(op_id: str, at: float) -> dict[str, Any]:
        pin = {"machine_id": "r", "earliest_start": at, "latest_start": at}
        return _op(op_id, "wc", ["r"], 2.0, pin=pin)

    request = _request(
        [_one_op_job("J1", pinned("x", 0.0)), _one_op_job("J2", pinned("y", 1.0))],
        [{"id": "r", "work_center_id": "wc"}],
    )

    result = solve(request)

    assert result.schedule.status == "UNKNOWN"
    assert not result.verified
    assert result.issues


def test_pinned_later_slot_is_reserved_and_free_work_fills_before_it() -> None:
    frozen = _op(
        "frozen",
        "wc",
        ["r"],
        2.0,
        pin={"machine_id": "r", "earliest_start": 10.0, "latest_start": 10.0},
    )
    jobs = [_one_op_job("F", frozen)] + [
        _one_op_job(f"J{name}", _op(name, "wc", ["r"], 2.0)) for name in ("a", "b", "c")
    ]
    request = _request(jobs, [{"id": "r", "work_center_id": "wc"}])

    result = solve(request)

    assert result.verified
    ends = {a.operation_id: a.end for a in result.schedule.assignments}
    assert _starts(result.schedule)["frozen"] == 10.0
    assert ends["c"] <= 10.0


# --- CP-SAT ----------------------------------------------------------------------------------


def _cpsat() -> Solver:
    return get_solver("ortools", workers=1, seed=7)


def test_cpsat_proves_the_hand_solved_optimum() -> None:
    result = solve(_hand_request(), solver=_cpsat(), budget_seconds=5)

    assert result.schedule.status == "OPTIMAL"
    assert result.verified
    assert result.schedule.primary_value == 5.0
    assert result.schedule.best_bound == 5.0
    assert result.objective_values[ObjectiveKind.MAKESPAN] == 5.0


def test_cpsat_reports_infeasible_when_the_calendar_is_too_short() -> None:
    request = _request(
        [_one_op_job("J", _op("x", "wc", ["r"], 2.0))],
        [{"id": "r", "work_center_id": "wc", "calendar_id": "c"}],
        calendars=[{"id": "c", "intervals": [(0.0, 1.0)]}],
    )

    result = solve(request, solver=_cpsat(), budget_seconds=5)

    assert result.schedule.status == "INFEASIBLE"
    assert not result.verified


def test_cpsat_keeps_fractional_seconds_exact() -> None:
    pin = {"machine_id": "r", "earliest_start": 0.0, "latest_start": 0.0}
    request = _request(
        [_one_op_job("J", _op("x", "wc", ["r"], 0.0015, pin=pin))],
        [{"id": "r", "work_center_id": "wc"}],
    )

    result = solve(request, solver=_cpsat(), budget_seconds=5)

    assert result.schedule.status == "OPTIMAL"
    assert result.verified
    assert result.schedule.assignments[0].end == pytest.approx(0.0015)


def test_cpsat_honors_a_pinned_machine_among_alternatives() -> None:
    pin = {"machine_id": "r2", "earliest_start": 4.0, "latest_start": 4.0}
    request = _request(
        [_one_op_job("J", _op("x", "wc", ["r1", "r2"], 1.0, pin=pin))],
        [{"id": "r1", "work_center_id": "wc"}, {"id": "r2", "work_center_id": "wc"}],
    )

    result = solve(request, solver=_cpsat(), budget_seconds=5)

    assert result.schedule.status == "OPTIMAL"
    assert [(a.machine_id, a.start) for a in result.schedule.assignments] == [("r2", 4.0)]
    assert result.features == (Feature.ALTERNATIVES.value, Feature.PINS.value)


def test_cpsat_minimizes_max_lateness_from_the_profile_objective() -> None:
    request = _request(
        [_one_op_job("J", _op("x", "wc", ["r"], 2.0), release=3.0, due=4.0)],
        [{"id": "r", "work_center_id": "wc"}],
        profile={"objective": {"mode": "lexicographic", "levels": [{"kind": "max_lateness"}]}},
    )

    result = solve(request, solver=_cpsat(), budget_seconds=5)

    assert result.schedule.status == "OPTIMAL"
    assert result.schedule.primary_value == 1.0
    assert result.objective_values[ObjectiveKind.MAX_LATENESS] == 1.0


@pytest.mark.parametrize("budget", [0.0, -1.0, math.inf, math.nan])
def test_invalid_budget_is_refused_before_any_solver_runs(budget: float) -> None:
    result = solve(_hand_request(), solver=_cpsat(), budget_seconds=budget)

    assert result.schedule.status == "UNKNOWN"
    assert [issue.code for issue in result.issues] == ["budget"]


def test_cpsat_tiny_budget_reports_unknown_with_a_time_limit_issue() -> None:
    machines = [{"id": f"r{k}", "work_center_id": "wc"} for k in range(4)]
    jobs = [
        _one_op_job(f"J{i}", _op(f"x{i}", "wc", [m["id"] for m in machines], 1.0))
        for i in range(80)
    ]

    result = solve(_request(jobs, machines), solver=_cpsat(), budget_seconds=1e-9)

    assert result.schedule.status == "UNKNOWN"
    assert not result.verified
    assert "time_limit" in {issue.code for issue in result.issues}


# --- input boundary and forged answers -------------------------------------------------------


def test_parse_request_is_strict_and_rejects_non_finite_numbers() -> None:
    raw = _hand_request().model_dump(mode="json")

    assert isinstance(parse_request(raw), SolveRequest)
    extra = parse_request({**raw, "extra": True})
    assert isinstance(extra, list) and {issue.code for issue in extra} == {"schema"}
    infinite = json.loads(json.dumps(raw))
    infinite["problem"]["jobs"][0]["release"] = math.inf
    issues = parse_request(infinite)
    assert isinstance(issues, list) and issues
    assert all(issue.code == "schema" for issue in issues)


def test_v1_problem_body_is_refused_naming_schema_version() -> None:
    v1 = {
        "operations": [{"id": "x", "duration": 1}],
        "resources": [{"resource_id": "r", "start": 0, "end": 2}],
    }

    issues = parse_request({"schema_version": "1.0", "problem": v1})

    assert isinstance(issues, list)
    assert "schema_version" in {issue.code for issue in issues}


class _ForgedSolver:
    """Claims a FEASIBLE plan on a machine the operation cannot use."""

    name = "forged"
    capabilities: frozenset[Feature] = frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        del problem, objective, budget_seconds, context
        wrong = Assignment(operation_id="cut", machine_id="b1", start=0.0, setup_end=0.0, end=2.0)
        return Schedule(
            status="FEASIBLE", assignments=(wrong,), solver=self.name, elapsed_seconds=0
        )


def test_forged_solver_answer_is_not_verified() -> None:
    result = solve(_hand_request(), solver=_ForgedSolver())

    assert not result.verified
    assert result.objective_values == {}
    codes = {issue.code for issue in result.issues}
    assert {"ineligible_machine", "missing_assignment"} <= codes


def test_verifier_flags_missing_unknown_and_ineligible_assignments() -> None:
    request = _hand_request()
    forged = Schedule(
        status="FEASIBLE",
        assignments=(
            Assignment(operation_id="cut", machine_id="b1", start=0, setup_end=0, end=2),
            Assignment(operation_id="ghost", machine_id="m1", start=0, setup_end=0, end=1),
        ),
        solver="forged",
        elapsed_seconds=0,
    )

    check = verify(apply_overrides(request), request.overrides, forged)

    assert not check.valid
    codes = {issue.code for issue in check.issues}
    assert {"missing_assignment", "unknown_assignment", "ineligible_machine"} <= codes


# --- translator (port of tests/unit/test_linear_schedule.py) --------------------------------


def _shared_operator_scenario() -> dict[str, Any]:
    """Two cut machines share one operator: v1 blocked this, v2 models the pool (D-063)."""
    model = {
        "defaults": {"cycle_time_cv": 0.0},
        "stocks": [],
        "part_types": [
            {"name": name, "uom": "piece", "attributes": {}} for name in ("blank", "finished")
        ],
        "machines": [{"name": "machine_cut"}],
        "labor": {"pools": [{"name": "operators", "headcount": 1, "skills": ["operator"]}]},
        "locations": [
            {
                "name": "cut",
                "consumes": [{"thing": "blank", "qty": 1, "uom": "piece"}],
                "emits": [{"thing": "finished", "qty": 1, "uom": "piece"}],
                "setup_key": "setup_cut",
                "time_model": {"kind": "rate_based", "rate": 0.5},
                "machine": "machine_cut",
                "capacity": 2,
                "labor_skill": "operator",
            }
        ],
        "routing": [{"part": "finished", "steps": ["cut"]}],
    }
    capsule = ScenarioCapsule.from_dict(
        {
            "schema_version": "0.1",
            "model": model,
            "snapshot": {
                "id": "snap",
                "as_of": "2026-10-08T00:00:00+00:00",
                "model_revision": "1",
                "production_plan": [
                    {
                        "work_order_id": "seed",
                        "part": "finished",
                        "qty": 1,
                        "start_date": "0",
                        "due_date": "100",
                    }
                ],
            },
            "experiment": {"id": "exp", "replications": 1, "seed": 1},
            "assumptions": [],
            "provenance": {"source": "test"},
            "required_capabilities": [],
        }
    )
    return {"id": "s", "digest": capsule.digest, "capsule": capsule.to_dict()}


def test_shared_operator_becomes_a_binding_pool_instead_of_a_block() -> None:
    scenario = _shared_operator_scenario()
    snapshot = {
        "scenario_id": "s",
        "scenario_digest": scenario["digest"],
        "orders": [
            {
                "order_id": order,
                "product": "finished",
                "quantity": 1,
                "release_second": 0.0,
                "due_second": 100.0,
            }
            for order in ("o1", "o2")
        ],
    }

    translation = build_request(scenario, snapshot)

    assert translation.status == "ready", translation.issues
    assert translation.request is not None
    problem = translation.request.problem
    assert [pool.size for pool in problem.operator_pools] == [1]
    assert {m.id for m in problem.machines} == {"machine_cut-1", "machine_cut-2"}
    result = solve(translation.request)
    assert result.verified, result.issues
    first, second = sorted(result.schedule.assignments, key=lambda a: a.start)
    assert second.start >= first.end
