"""Solver seam tests with fake solvers (T-099, T-114, COMP-001, COMP-008, REQ-072, REQ-073,
REQ-087, D-067, D-068, D-070).

A fake solver records what `solve()` hands it and returns a schedule chosen by the test,
so the seam is tested without any real algorithm: feature derivation, capability refusal,
budget checks, the `SolveContext` the solver receives, verification of the returned
schedule and objective evaluation.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from twinflow.scheduling import overrides, registry
from twinflow.scheduling.features import problem_features
from twinflow.scheduling.model import (
    Assignment,
    FrozenZone,
    HoldOverride,
    Job,
    Machine,
    ObjectiveKind,
    ObjectiveSpec,
    Operation,
    Override,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    Schedule,
    SchedulingProfile,
    SetupFunction,
    SolveRequest,
    WeightedSum,
    WeightedTerm,
    WorkCenter,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.seam import Feature, Solver, enforces_budget
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

if TYPE_CHECKING:
    from twinflow.scheduling.seam import SolveContext

WC = "WC"
ALL_FEATURES = frozenset(Feature)


# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, *machines: str, quantity: float = 1.0, **fields: Any) -> Operation:
    """An operation with 5 s of fixed run on each listed machine (M1 when none given)."""
    options = [
        {"machine_id": m, "fixed_seconds": 5.0, "unit_seconds": 0.0, "preference_cost": 0.0}
        for m in machines or ("M1",)
    ]
    data = {"id": op_id, "work_center_id": WC, "quantity": quantity, "options": options}
    return Operation.model_validate(data | fields)


def _machine(machine_id: str = "M1", **fields: Any) -> Machine:
    return Machine.model_validate({"id": machine_id, "work_center_id": WC} | fields)


def _problem(
    operations: Sequence[Operation] = (),
    machines: Sequence[Machine] = (),
    **fields: Any,
) -> Problem:
    """One job `J` over `operations` (default: one plain op `A` on M1)."""
    job = Job(id="J", operations=tuple(operations) or (_op("A"),))
    data: dict[str, Any] = {
        "id": "p",
        "jobs": (job,),
        "machines": tuple(machines) or (_machine(),),
        "work_centers": (WorkCenter(id=WC),),
    }
    return Problem.model_validate(data | fields)


def _chain(**link: float) -> Problem:
    """Op A then op B (both quantity 2) on M1, B after A with `link` lag fields."""
    first = _op("A", quantity=2.0)
    second = _op("B", quantity=2.0, predecessors=[{"operation_id": "A", **link}])
    return _problem((first, second))


SETUP_FN = SetupFunction(id="s", terms=(), default_seconds=3.0)
CALENDAR = {"id": "c", "intervals": [(0.0, 100.0)]}
POOL = {"id": "P", "size": 1, "calendar_id": "c"}
NEED = [{"pool_id": "P", "quantity": 1, "phase": "both"}]


def _with_setup(op: Operation) -> Problem:
    return _problem((op,), (_machine(setup_function_id="s"),), setup_functions=(SETUP_FN,))


def _request(
    problem: Problem,
    overrides: Sequence[Override] = (),
    profile: SchedulingProfile | None = None,
) -> SolveRequest:
    return SolveRequest(
        schema_version="2.0",
        problem=problem,
        overrides=tuple(overrides),
        profile=profile or SchedulingProfile(),
    )


# --- fake solver ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One `solve` call; `context` and `called_at` (monotonic clock) are not compared."""

    problem: Problem
    objective: ObjectiveSpec
    budget_seconds: float
    context: SolveContext | None = field(default=None, compare=False)
    called_at: float = field(default=0.0, compare=False)


def _valid_schedule(problem: Problem) -> Schedule:
    """Every op back to back on M1 from 0, 5 s each, no setup (valid on the plain problems)."""
    ops = [op for job in problem.jobs for op in job.operations]
    assignments = tuple(
        Assignment(
            operation_id=op.id, machine_id="M1", start=5.0 * k, setup_end=5.0 * k, end=5.0 * (k + 1)
        )
        for k, op in enumerate(ops)
    )
    return Schedule(status="FEASIBLE", assignments=assignments, solver="fake", elapsed_seconds=0.0)


@dataclass
class FakeSolver:
    """Records every call and answers with `answer(problem)`."""

    capabilities: frozenset[Feature] = ALL_FEATURES
    answer: Callable[[Problem], Schedule] = _valid_schedule
    name: str = "fake"
    calls: list[Call] = field(default_factory=list)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        self.calls.append(Call(problem, objective, budget_seconds, context, time.monotonic()))
        return self.answer(problem)


def test_fake_solver_satisfies_the_solver_protocol() -> None:
    solver: Solver = FakeSolver()
    assert solver.name == "fake"


# --- feature derivation --------------------------------------------------------------------


SINGLE_FEATURE_PROBLEMS: dict[Feature, tuple[Problem, frozenset[Feature]]] = {
    Feature.ALTERNATIVES: (
        _problem((_op("A", "M1", "M2"),), (_machine("M1"), _machine("M2"))),
        frozenset({Feature.ALTERNATIVES}),
    ),
    Feature.SETUPS: (_with_setup(_op("A")), frozenset({Feature.SETUPS})),
    # A detached op that can get a setup is also a setup problem: the pair is the minimum.
    Feature.DETACHED_SETUP: (
        _with_setup(_op("A", setup_detached=True)),
        frozenset({Feature.SETUPS, Feature.DETACHED_SETUP}),
    ),
    Feature.CALENDARS: (
        _problem(machines=(_machine(calendar_id="c"),), calendars=(CALENDAR,)),
        frozenset({Feature.CALENDARS}),
    ),
    # Pause only matters where a calendar can apply (review item 7): the pair is the minimum.
    Feature.PAUSE: (
        _problem(
            (_op("A", shift_crossing="pause"),),
            (_machine(calendar_id="c"),),
            calendars=(CALENDAR,),
        ),
        frozenset({Feature.PAUSE, Feature.CALENDARS}),
    ),
    Feature.UNATTENDED: (
        _problem((_op("A", shift_crossing="finish_unattended"),)),
        frozenset({Feature.UNATTENDED}),
    ),
    Feature.OPERATORS: (
        _problem((_op("A", operator_needs=NEED),), operator_pools=({"id": "P", "size": 1},)),
        frozenset({Feature.OPERATORS}),
    ),
    Feature.BATCHING: (
        _problem(
            (_op("A", batch_family="oven"),),
            (_machine(batch={"capacity": 10.0, "unit": "kg"}),),
        ),
        frozenset({Feature.BATCHING}),
    ),
    Feature.RESTART: (
        _problem(
            machines=(_machine(restart={"idle_threshold_seconds": 60.0, "warmup_seconds": 5.0}),)
        ),
        frozenset({Feature.RESTART}),
    ),
    Feature.LAGS: (_chain(min_lag=2.0), frozenset({Feature.LAGS})),
    Feature.MAX_LAGS: (_chain(max_lag=10.0), frozenset({Feature.MAX_LAGS})),
    Feature.OVERLAP: (_chain(overlap_quantity=1.0), frozenset({Feature.OVERLAP})),
    Feature.PINS: (_problem((_op("A", pin={"machine_id": "M1"}),)), frozenset({Feature.PINS})),
    Feature.IN_PROGRESS: (
        _problem(
            (
                _op(
                    "A",
                    status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 2.0},
                ),
            )
        ),
        frozenset({Feature.IN_PROGRESS}),
    ),
    Feature.MACHINE_STATE: (
        _problem(machines=(_machine(state={"available_from": 5.0}),)),
        frozenset({Feature.MACHINE_STATE}),
    ),
}


def test_single_feature_table_covers_every_feature() -> None:
    assert set(SINGLE_FEATURE_PROBLEMS) == set(Feature)


@pytest.mark.parametrize("feature", list(Feature), ids=str)
def test_single_feature_problem_derives_exactly_that_feature(feature: Feature) -> None:
    problem, expected = SINGLE_FEATURE_PROBLEMS[feature]
    assert validate(_request(problem)) == []
    assert problem_features(problem) == expected


def test_plain_problem_has_no_features() -> None:
    assert problem_features(_problem()) == frozenset()
    assert problem_features(_chain()) == frozenset()


def test_detached_flag_without_any_setup_function_is_not_a_feature() -> None:
    assert problem_features(_problem((_op("A", setup_detached=True),))) == frozenset()


def test_setup_function_on_the_work_center_counts_as_setups() -> None:
    problem = _problem(
        work_centers=(WorkCenter(id=WC, setup_function_id="s"),), setup_functions=(SETUP_FN,)
    )
    assert problem_features(problem) == frozenset({Feature.SETUPS})


def test_setup_function_on_an_unused_machine_is_not_a_feature() -> None:
    machines = (_machine("M1"), _machine("M2", setup_function_id="s"))
    problem = _problem(machines=machines, setup_functions=(SETUP_FN,))
    assert problem_features(problem) == frozenset()


def test_pool_calendar_counts_only_when_the_pool_is_used() -> None:
    unused = _problem(operator_pools=(POOL,), calendars=(CALENDAR,))
    used = _problem((_op("A", operator_needs=NEED),), operator_pools=(POOL,), calendars=(CALENDAR,))
    assert problem_features(unused) == frozenset()
    assert problem_features(used) == frozenset({Feature.OPERATORS, Feature.CALENDARS})


def test_batch_machine_without_batch_family_ops_is_not_batching() -> None:
    problem = _problem(machines=(_machine(batch={"capacity": 10.0, "unit": "kg"}),))
    assert problem_features(problem) == frozenset()


def test_machine_state_counts_last_setup_but_not_availability_at_or_before_now() -> None:
    idle_now = _problem(machines=(_machine(state={"available_from": 5.0}),), now=5.0)
    last_family = _problem(machines=(_machine(state={"last_setup_family": "f"}),))
    last_params = _problem(machines=(_machine(state={"last_setup_params": {"w": 1.0}}),))
    assert problem_features(idle_now) == frozenset()
    assert problem_features(last_family) == frozenset({Feature.MACHINE_STATE})
    assert problem_features(last_params) == frozenset({Feature.MACHINE_STATE})


def test_done_operations_contribute_no_features() -> None:
    done = _op("A", pin={"machine_id": "M1"}, status={"state": "done"})
    assert problem_features(_problem((done, _op("B")))) == frozenset()


# --- pause needs a calendar that can apply (review item 7) ---------------------------------


PAUSED = _op("A", "M1", "M2", shift_crossing="pause")


def test_pause_without_any_calendar_is_not_a_feature() -> None:
    problem = _problem((PAUSED,), (_machine("M1"), _machine("M2")))
    assert validate(_request(problem)) == []
    assert problem_features(problem) == frozenset({Feature.ALTERNATIVES})


def test_pause_with_a_calendar_on_one_option_machine_is_a_feature() -> None:
    problem = _problem(
        (PAUSED,), (_machine("M1"), _machine("M2", calendar_id="c")), calendars=(CALENDAR,)
    )
    assert validate(_request(problem)) == []
    assert problem_features(problem) == frozenset(
        {Feature.ALTERNATIVES, Feature.CALENDARS, Feature.PAUSE}
    )


def test_pause_with_a_calendar_only_on_a_machine_the_op_cannot_use_is_not_a_feature() -> None:
    other = _op("B", "M3")
    machines = (_machine("M1"), _machine("M2"), _machine("M3", calendar_id="c"))
    problem = _problem((PAUSED, other), machines, calendars=(CALENDAR,))
    assert validate(_request(problem)) == []
    assert problem_features(problem) == frozenset({Feature.ALTERNATIVES, Feature.CALENDARS})


def test_pause_drawing_operators_from_a_pool_with_a_calendar_is_a_feature() -> None:
    op = _op("A", shift_crossing="pause", operator_needs=NEED)
    problem = _problem((op,), operator_pools=(POOL,), calendars=(CALENDAR,))
    assert validate(_request(problem)) == []
    assert problem_features(problem) == frozenset(
        {Feature.OPERATORS, Feature.CALENDARS, Feature.PAUSE}
    )


def test_pause_with_a_calendar_only_on_a_pool_the_op_does_not_draw_from_is_not_a_feature() -> None:
    paused = _op(
        "A",
        shift_crossing="pause",
        operator_needs=[{"pool_id": "Q", "quantity": 1, "phase": "both"}],
    )
    pools = (POOL, {"id": "Q", "size": 1})
    other = _op("B", operator_needs=NEED)
    problem = _problem((paused, other), operator_pools=pools, calendars=(CALENDAR,))
    assert validate(_request(problem)) == []
    assert problem_features(problem) == frozenset({Feature.OPERATORS, Feature.CALENDARS})


# --- what the solver receives ---------------------------------------------------------------


def test_solver_receives_compiled_problem_and_profile_objective_only() -> None:
    objective = WeightedSum(terms=(WeightedTerm(kind=ObjectiveKind.TOTAL_TARDINESS, weight=1.0),))
    profile = SchedulingProfile(objective=objective, time_budget_seconds=7.0)
    hold = HoldOverride(id="h", fact_ref="f", reason="r", hardness="hard", job_id="K")
    problem = _problem().model_copy(
        update={"jobs": (*_problem().jobs, Job(id="K", operations=(_op("X"),)))}
    )
    request = _request(problem, overrides=(hold,), profile=profile)
    fake = FakeSolver()

    result = solve(request, solver=fake)

    assert fake.calls == [Call(apply_overrides(request).problem, objective, 7.0)]
    assert [job.id for job in fake.calls[0].problem.jobs] == ["J"]
    assert result.held_job_ids == ("K",)


def test_explicit_budget_wins_over_profile_budget() -> None:
    fake = FakeSolver()
    solve(_request(_problem()), solver=fake, budget_seconds=1.5)
    assert fake.calls[0].budget_seconds == 1.5


# --- the SolveContext the solver receives (T-114, REQ-087, D-070) ---------------------------


DEADLINE_SLACK = 0.05
PREVIOUS = PreviousPlan(
    plan_id="prev",
    problem_digest="d",
    assignments=(PreviousAssignment(operation_id="A", machine_id="M1", start=20.0),),
)


def _context(
    request: SolveRequest, fake: FakeSolver | None = None, **solve_kwargs: Any
) -> tuple[SolveContext, Call]:
    """Run `solve` with a fake and return the one context it received."""
    solver = fake or FakeSolver()
    result = solve(request, solver=solver, **solve_kwargs)
    assert len(solver.calls) == 1, result.issues
    call = solver.calls[0]
    assert call.context is not None
    return call.context, call


def _with_previous(request: SolveRequest) -> SolveRequest:
    return request.model_copy(update={"previous_plan": PREVIOUS})


def test_context_previous_plan_is_the_request_previous_plan() -> None:
    context, _ = _context(_with_previous(_request(_problem())))
    assert context.previous_plan == PREVIOUS


def test_context_previous_plan_is_none_without_one() -> None:
    context, _ = _context(_request(_problem()))
    assert context.previous_plan is None


def test_context_churn_tolerance_is_the_profile_frozen_zone_tolerance() -> None:
    profile = SchedulingProfile(frozen_zone=FrozenZone(until=50.0, tolerance=3.5))
    context, _ = _context(_with_previous(_request(_problem(), profile=profile)))
    assert context.churn_tolerance == 3.5


def test_context_churn_tolerance_is_zero_without_a_frozen_zone() -> None:
    context, _ = _context(_with_previous(_request(_problem())))
    assert context.churn_tolerance == 0.0


def test_context_seed_and_warm_start_are_what_solve_was_given() -> None:
    warm = _valid_schedule(_problem())
    context, _ = _context(_request(_problem()), seed=17, warm_start=warm)
    assert context.seed == 17
    assert context.warm_start == warm


def test_context_seed_defaults_to_zero_and_warm_start_to_none() -> None:
    context, _ = _context(_request(_problem()))
    assert context.seed == 0
    assert context.warm_start is None


def test_context_deadline_is_monotonic_now_plus_the_explicit_budget() -> None:
    before = time.monotonic()
    context, call = _context(_request(_problem()), budget_seconds=7.0)
    assert before + 7.0 <= context.deadline <= call.called_at + 7.0
    assert abs(context.deadline - (call.called_at + 7.0)) <= DEADLINE_SLACK


def test_context_deadline_uses_the_profile_budget_when_none_is_given() -> None:
    profile = SchedulingProfile(time_budget_seconds=12.0)
    before = time.monotonic()
    context, call = _context(_request(_problem(), profile=profile))
    assert before + 12.0 <= context.deadline <= call.called_at + 12.0
    assert abs(context.deadline - (call.called_at + 12.0)) <= DEADLINE_SLACK


def test_context_params_are_the_profile_method_params_for_the_solver_name() -> None:
    profile = SchedulingProfile.model_validate(
        {"method_params": {"atcs": {"K1": 5.0, "mode": "active"}, "fake": {"iters": 3}}}
    )
    context, _ = _context(_request(_problem(), profile=profile), FakeSolver(name="atcs"))
    assert isinstance(context.params, Mapping)
    assert dict(context.params) == {"K1": 5.0, "mode": "active"}


def test_context_params_are_empty_when_the_profile_has_none_for_the_solver() -> None:
    profile = SchedulingProfile.model_validate({"method_params": {"atcs": {"K1": 5.0}}})
    context, _ = _context(_request(_problem(), profile=profile))
    assert isinstance(context.params, Mapping)
    assert dict(context.params) == {}


def test_context_params_are_empty_with_the_default_profile() -> None:
    context, _ = _context(_request(_problem()))
    assert dict(context.params) == {}


# --- capability refusal ---------------------------------------------------------------------


def test_solver_lacking_setups_is_never_called_on_a_setup_problem() -> None:
    fake = FakeSolver(capabilities=ALL_FEATURES - {Feature.SETUPS})

    result = solve(_request(_with_setup(_op("A"))), solver=fake)

    assert fake.calls == []
    assert result.schedule.status == "UNKNOWN"
    assert result.schedule.assignments == ()
    assert result.verified is False
    assert [(i.code, i.path) for i in result.issues] == [("unsupported_feature", "problem")]
    assert "setups" in result.issues[0].message
    assert result.objective_values == {}
    assert result.features == ("setups",)


def test_one_unsupported_feature_issue_per_missing_feature_in_sorted_order() -> None:
    fake = FakeSolver(capabilities=ALL_FEATURES - {Feature.SETUPS, Feature.DETACHED_SETUP})

    result = solve(_request(_with_setup(_op("A", setup_detached=True))), solver=fake)

    assert fake.calls == []
    assert [i.code for i in result.issues] == ["unsupported_feature"] * 2
    assert "detached_setup" in result.issues[0].message
    assert "setups" in result.issues[1].message


def test_restart_rule_is_refused_by_a_solver_with_every_other_feature() -> None:
    problem, _ = SINGLE_FEATURE_PROBLEMS[Feature.RESTART]
    fake = FakeSolver(capabilities=ALL_FEATURES - {Feature.RESTART})

    result = solve(_request(problem), solver=fake)

    assert fake.calls == []
    assert result.schedule.status == "UNKNOWN"
    assert [i.code for i in result.issues] == ["unsupported_feature"]
    assert "restart" in result.issues[0].message


# --- verification and evaluation -------------------------------------------------------------


def test_verified_result_reports_all_thirteen_objective_values() -> None:
    problem = _chain()
    result = solve(_request(problem), solver=FakeSolver(capabilities=frozenset()))

    assert result.verified is True
    assert result.issues == ()
    assert result.schedule == _valid_schedule(problem)
    assert set(result.objective_values) == set(ObjectiveKind)
    assert result.objective_values[ObjectiveKind.MAKESPAN] == 10.0
    assert result.features == ()


def test_features_are_reported_as_sorted_strings() -> None:
    problem = _problem(
        (_op("A", pin={"machine_id": "M1"}, shift_crossing="pause"),),
        (_machine(calendar_id="c"),),
        calendars=(CALENDAR,),
    )
    result = solve(_request(problem), solver=FakeSolver())
    assert result.features == ("calendars", "pause", "pins")


def _forged(problem: Problem) -> Schedule:
    """A run of 1 s where the option needs 5 s."""
    op = problem.jobs[0].operations[0]
    forged = Assignment(operation_id=op.id, machine_id="M1", start=0.0, setup_end=0.0, end=1.0)
    return Schedule(status="OPTIMAL", assignments=(forged,), solver="fake", elapsed_seconds=0.0)


def test_forged_schedule_is_reported_unverified_with_verifier_issues() -> None:
    request = _request(_problem())

    result = solve(request, solver=FakeSolver(answer=_forged))

    expected = verify(apply_overrides(request), (), _forged(request.problem)).issues
    assert result.verified is False
    assert expected
    assert result.issues == expected
    assert result.objective_values == {}
    assert result.schedule.status == "OPTIMAL"


@pytest.mark.parametrize("status", ["UNKNOWN", "INFEASIBLE"])
def test_non_solution_status_is_not_verified_and_keeps_solver_issues(status: str) -> None:
    issue = {"code": "no_feasible_slot", "path": "problem", "message": "no slot"}

    def answer(_: Problem) -> Schedule:
        return Schedule.model_validate(
            {
                "status": status,
                "assignments": (),
                "solver": "fake",
                "elapsed_seconds": 0.0,
                "issues": [issue],
            }
        )

    result = solve(_request(_problem()), solver=FakeSolver(answer=answer))

    assert result.verified is False
    assert [i.code for i in result.issues] == ["no_feasible_slot"]
    assert result.objective_values == {}


def test_solve_turns_a_solver_exception_into_unknown_solver_error() -> None:
    def answer(_: Problem) -> Schedule:
        raise RuntimeError("secret internal state /etc/passwd")

    result = solve(_request(_problem()), solver=FakeSolver(answer=answer))

    assert result.schedule.status == "UNKNOWN"
    assert not result.verified
    (issue,) = result.issues
    assert (issue.code, issue.path, issue.message) == ("solver_error", "solver", "RuntimeError")


# --- request and budget validation ------------------------------------------------------------


def test_invalid_request_returns_validator_issues_without_calling_solver() -> None:
    request = _request(_problem((_op("A", "M9"),)))
    fake = FakeSolver()

    result = solve(request, solver=fake)

    assert fake.calls == []
    assert result.schedule.status == "UNKNOWN"
    assert result.verified is False
    assert result.issues == tuple(validate(request))
    assert result.issues


@pytest.mark.parametrize("budget", [0.0, -1.0, math.nan, math.inf, -math.inf])
def test_budget_not_positive_and_finite_is_rejected(budget: float) -> None:
    fake = FakeSolver()

    result = solve(_request(_problem()), solver=fake, budget_seconds=budget)

    assert fake.calls == []
    assert result.schedule.status == "UNKNOWN"
    assert [(i.code, i.path) for i in result.issues] == [("budget", "budget_seconds")]


# --- frozen zone is verified independently (TF-002 review) ----------------------------------


def _frozen_request() -> SolveRequest:
    """A on M1, frozen at its previous start 20 (zone until 50, tolerance 0)."""
    previous = PreviousPlan(
        plan_id="prev",
        problem_digest="d",
        assignments=(PreviousAssignment(operation_id="A", machine_id="M1", start=20.0),),
    )
    profile = SchedulingProfile(frozen_zone=FrozenZone(until=50.0, tolerance=0.0))
    return _request(_problem(), profile=profile).model_copy(update={"previous_plan": previous})


def test_solve_keeps_a_frozen_op_at_its_previous_start_and_verifies() -> None:
    result = solve(_frozen_request())

    assert result.verified, result.issues
    (a,) = result.schedule.assignments
    assert (a.machine_id, a.start) == ("M1", 20.0)


def test_solve_with_frozen_pins_lost_in_compile_does_not_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: drop the compile step's frozen pins; the verifier still enforces the zone."""
    monkeypatch.setattr(overrides, "_apply_frozen_zone", lambda state, request: None)

    result = solve(_frozen_request())

    assert result.schedule.status == "FEASIBLE"
    assert not result.verified
    assert [issue.code for issue in result.issues] == ["frozen"]


# --- restart rule through the default solver (T-116, D-074, REQ-107) -------------------------


def test_default_solver_supports_restart_and_its_schedule_verifies() -> None:
    restart = {"idle_threshold_seconds": 10.0, "warmup_seconds": 5.0}
    first = _op("A")
    second = _op("B", predecessors=[{"operation_id": "A", "min_lag": 20.0}])
    problem = _problem((first, second), (_machine(restart=restart),))

    result = solve(_request(problem))

    assert "unsupported_feature" not in [i.code for i in result.issues], result.issues
    assert "restart" in result.features
    assert result.verified is True, result.issues
    by_op = {a.operation_id: a for a in result.schedule.assignments}
    assert (by_op["B"].start, by_op["B"].setup_end, by_op["B"].end) == (25.0, 30.0, 35.0)


def test_default_solver_verifies_the_single_feature_restart_problem() -> None:
    problem, _ = SINGLE_FEATURE_PROBLEMS[Feature.RESTART]

    result = solve(_request(problem))

    assert [i.code for i in result.issues] == []
    assert result.verified is True


# --- whether the solver honored the budget (time_limit_applied) ------------------------------


def test_budget_enforced_is_what_the_solver_reports() -> None:
    honoring = FakeSolver(
        answer=lambda p: _valid_schedule(p).model_copy(update={"budget_enforced": True})
    )

    assert solve(_request(_problem()), solver=honoring).schedule.budget_enforced is True
    assert solve(_request(_problem()), solver=FakeSolver()).schedule.budget_enforced is False


def test_budget_enforced_is_false_when_the_solver_never_ran() -> None:
    result = solve(_request(_problem()), solver=FakeSolver(), budget_seconds=0.0)

    assert result.schedule.budget_enforced is False


@pytest.mark.parametrize("solver_name", ["baseline", "ortools"])
def test_builtin_solvers_report_that_they_enforce_the_budget(solver_name: str) -> None:
    from twinflow.scheduling import BaselineSolver
    from twinflow.scheduling.algorithms.cpsat import ORToolsSolver  # D-069: off the facade

    chosen: Solver = BaselineSolver() if solver_name == "baseline" else ORToolsSolver()

    result = solve(_request(_problem()), solver=chosen, budget_seconds=5.0)

    assert result.schedule.budget_enforced is True


# Solvers that cannot be interrupted: they never read `SolveContext.deadline`.
UNINTERRUPTIBLE_SOLVERS = frozenset({"rush_insert"})


@pytest.mark.parametrize("info", registry.available(), ids=lambda info: info.name)
def test_every_registered_solver_reports_budget_enforcement_truthfully(
    info: registry.SolverInfo,
) -> None:
    """A solver that checks the deadline enforces the budget; one that cannot stop does not."""
    solver = registry.get(info.name)
    expected = info.name not in UNINTERRUPTIBLE_SOLVERS

    assert enforces_budget(solver) is expected

    result = solve(_request(_problem()), solver=solver, budget_seconds=5.0)

    assert result.schedule.budget_enforced is expected
