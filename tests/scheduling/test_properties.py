"""Property suite: 200 seeded problems through every solver (T-104, T-128, COMP-012, COMP-027,
REQ-077, REQ-089).

Each seed's request comes from `generators.random_request`. Every solver in
`registry.available()` runs on every seed, so a solver registered later joins the suite with
no edit here; one whose optional dependency is missing is skipped. For each of them: nothing
raises (no `solver_error`), every FEASIBLE or OPTIMAL answer verifies, an UNKNOWN or
INFEASIBLE answer carries a named issue, and a refusal names exactly the features the
solver's registry entry does not declare. Method-specific properties, per seed:

- the baseline never answers INFEASIBLE (it cannot prove infeasibility);
- every FEASIBLE or OPTIMAL answer of the baseline and of CP-SAT verifies;
- a refusal for features names exactly the features the solver lacks;
- CP-SAT never calls a problem INFEASIBLE that the baseline solved, and an OPTIMAL CP-SAT
  answer is no worse than the baseline on the primary objective.

Across the seeds every `Feature` and every override kind appears, and enough answers
verify that the properties are not vacuous. Every failure message names the seed.
"""

from __future__ import annotations

import functools
import re

import pytest
from generators import OVERRIDE_KINDS, random_request

from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.model import ScheduleIssue, SolveRequest, SolveResult
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.registry import SolverInfo, available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, OptionalDependencyError, ParamValue
from twinflow.scheduling.solver import SOLUTION_STATUSES, problem_features, solve
from twinflow.scheduling.validator import validate

SEEDS = range(200)
DETERMINISM_SEEDS = range(0, 200, 10)
PROPERTY_BUDGET_SECONDS = 1.0
REGISTERED = available()
# Construction params per solver for this suite; later improvement methods add an iteration
# limit here to keep the suite under its runtime budget (T-128).
SUITE_PARAMS: dict[str, dict[str, ParamValue]] = {"ortools": {"workers": 1, "seed": 0}}
TOLERANCE = 1e-6
MIN_BASELINE_VERIFIED = 150
MIN_CPSAT_VERIFIED = 50
_UNSUPPORTED = re.compile(r"does not support feature (\w+)$")


@functools.cache
def _request(seed: int) -> SolveRequest:
    return random_request(seed)


@functools.cache
def _registered(name: str, seed: int) -> SolveResult:
    """The registered solver `name` on seed `seed`; skips when its optional dependency is absent."""
    try:
        solver = get_solver(name, **SUITE_PARAMS.get(name, {}))
    except OptionalDependencyError as error:
        pytest.skip(f"{name}: {error}")
    return solve(_request(seed), solver=solver, budget_seconds=PROPERTY_BUDGET_SECONDS)


def _baseline(seed: int) -> SolveResult:
    return _registered("baseline", seed)


def _cpsat(seed: int) -> SolveResult:
    return _registered("ortools", seed)


def _compiled_features(seed: int) -> frozenset[Feature]:
    return problem_features(apply_overrides(_request(seed)).problem)


def _issue_codes(result: SolveResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _named_unsupported(result: SolveResult) -> set[str]:
    named = set()
    for issue in result.issues:
        if issue.code == "unsupported_feature":
            match = _UNSUPPORTED.search(issue.message)
            assert match is not None, f"unparseable unsupported_feature message: {issue.message}"
            named.add(match.group(1))
    return named


def _compiled_issues(seed: int) -> list[ScheduleIssue]:
    return validate(apply_overrides(_request(seed)))


def _assert_contract(seed: int, result: SolveResult, capabilities: frozenset[Feature]) -> None:
    """Refusals name exactly the missing features; every plan verifies."""
    who = f"seed {seed} ({result.schedule.solver})"
    compiled_issues = _compiled_issues(seed)
    if compiled_issues:
        assert result.issues == tuple(compiled_issues), f"{who}: {result.issues}"
        assert result.schedule.status == "UNKNOWN" and not result.verified, who
        return
    missing = {f.value for f in _compiled_features(seed) - capabilities}
    assert _named_unsupported(result) == missing, f"{who}: issues {result.issues}"
    if missing:
        assert result.schedule.status == "UNKNOWN", who
        assert result.schedule.assignments == (), who
        assert not result.verified, who
    if result.schedule.status in SOLUTION_STATUSES:
        assert result.verified, f"{who}: {result.schedule.status} did not verify: {result.issues}"
        assert result.objective_values, who


# --- the generator ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_generator_builds_a_valid_request(seed: int) -> None:
    assert validate(_request(seed)) == [], f"seed {seed}: generator built an invalid request"


@pytest.mark.parametrize("seed", SEEDS)
def test_valid_request_compiles_to_a_valid_problem(seed: int) -> None:
    issues = _compiled_issues(seed)

    assert issues == [], f"seed {seed}: valid request compiled to an invalid problem: {issues}"


def test_generator_is_deterministic() -> None:
    for seed in DETERMINISM_SEEDS:
        assert random_request(seed) == random_request(seed), f"seed {seed}"


def test_generated_problems_cover_every_feature() -> None:
    seen: set[Feature] = set()
    for seed in SEEDS:
        seen |= problem_features(_request(seed).problem)
    assert seen == set(Feature), f"never generated: {sorted(set(Feature) - seen)}"


def test_generated_requests_cover_every_override_kind_and_the_frozen_zone() -> None:
    kinds = {override.kind for seed in SEEDS for override in _request(seed).overrides}
    frozen = [seed for seed in SEEDS if _request(seed).profile.frozen_zone is not None]
    assert kinds == set(OVERRIDE_KINDS), f"never generated: {set(OVERRIDE_KINDS) - kinds}"
    assert frozen, "no seed has a frozen zone"
    assert all(_request(seed).previous_plan is not None for seed in frozen)


# --- every registered solver ---------------------------------------------------------------


@pytest.mark.parametrize("info", REGISTERED, ids=lambda info: info.name)
@pytest.mark.parametrize("seed", SEEDS)
def test_every_registered_solver_keeps_the_contract(seed: int, info: SolverInfo) -> None:
    result = _registered(info.name, seed)
    who = f"seed {seed} ({info.name})"

    assert "solver_error" not in _issue_codes(result), f"{who} raised: {result.issues}"
    if result.schedule.status not in SOLUTION_STATUSES:
        assert result.issues, f"{who}: {result.schedule.status} without a named issue"
    _assert_contract(seed, result, info.capabilities)


def test_the_suite_covers_the_whole_registry() -> None:
    assert {info.name for info in REGISTERED} >= {"baseline", "ortools"}
    assert [info.name for info in REGISTERED] == [info.name for info in available()]


# --- baseline --------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_baseline_never_infeasible_and_every_plan_verifies(seed: int) -> None:
    result = _baseline(seed)

    assert result.schedule.status != "INFEASIBLE", f"seed {seed}: {result.issues}"
    _assert_contract(seed, result, BASELINE_CAPABILITIES)


@pytest.mark.parametrize("seed", DETERMINISM_SEEDS)
def test_baseline_is_deterministic(seed: int) -> None:
    first = solve(_request(seed), solver=BaselineSolver())
    second = solve(_request(seed), solver=BaselineSolver())

    assert first.schedule.status == second.schedule.status, f"seed {seed}"
    assert first.schedule.assignments == second.schedule.assignments, f"seed {seed}"
    assert first.objective_values == second.objective_values, f"seed {seed}"


def test_baseline_verifies_on_most_seeds() -> None:
    verified = [seed for seed in SEEDS if _baseline(seed).verified]
    assert len(verified) >= MIN_BASELINE_VERIFIED, f"only {len(verified)} seeds verified"


# --- CP-SAT ----------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", SEEDS)
def test_cpsat_refuses_exactly_or_every_plan_verifies(seed: int) -> None:
    result = _cpsat(seed)

    _assert_contract(seed, result, get_solver("ortools").capabilities)


@pytest.mark.parametrize("seed", SEEDS)
def test_cpsat_agrees_with_a_verified_baseline(seed: int) -> None:
    cpsat_result, baseline_result = _cpsat(seed), _baseline(seed)
    if not baseline_result.verified:
        return
    status = cpsat_result.schedule.status
    assert status != "INFEASIBLE", f"seed {seed}: baseline plan exists: {cpsat_result.issues}"
    if status == "OPTIMAL":
        kind = primary_kind(_request(seed).profile.objective)
        best, heuristic = (r.objective_values[kind] for r in (cpsat_result, baseline_result))
        assert best <= heuristic + TOLERANCE, f"seed {seed}: {kind} {best} > {heuristic}"


def test_cpsat_runs_and_verifies_on_a_share_of_seeds() -> None:
    verified = [seed for seed in SEEDS if _cpsat(seed).verified]
    assert len(verified) >= MIN_CPSAT_VERIFIED, f"only {len(verified)} CP-SAT seeds verified"


def test_restart_is_supported_by_the_baseline_and_cpsat() -> None:
    restart_seeds = [s for s in SEEDS if Feature.RESTART in _compiled_features(s)]
    assert restart_seeds, "no seed uses restart"
    for seed in restart_seeds:
        assert "restart" not in _named_unsupported(_baseline(seed)), f"seed {seed}"
        cpsat = _cpsat(seed)
        assert "restart" not in _named_unsupported(cpsat), f"seed {seed}: {_issue_codes(cpsat)}"
