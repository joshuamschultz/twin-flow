"""Property suite: seeded problems through every solver (T-104, T-128, T-165, COMP-002,
COMP-012, COMP-027, REQ-077, REQ-089).

Each seed's request comes from `generators.random_request`. Every solver in
`registry.available()` runs on every seed of the sweep (seeds 0-1499: 0-199 in the default
run, 200-1499 marked `slow`), so a solver registered later joins the suite with no edit
here; one whose optional dependency is missing is skipped. The sweep is one test per solver
that loops its seeds and names the first failing seed with its issue codes, so 1500 seeds x
every solver stays a few dozen test items. For each answer: nothing raises (no
`solver_error`), every FEASIBLE or OPTIMAL answer verifies (the verifier checks every hard
override against the raw list), an UNKNOWN or INFEASIBLE answer carries a named issue, a
refusal names exactly the features the solver's registry entry does not declare, and on a
request with a frozen zone and a previous plan every frozen op keeps its machine and its
start within the tolerance (T-165). The frozen set is computed here from the raw request
by the TF-002 DEV-16 rule, independently of the verifier: a previous assignment starting
before `until` whose op is still solved (in the compiled problem, pending, job open and not
held) and that no raw hard lock on it, hard exclude of its previous machine or hard prefer
of another machine releases.

Reactive methods (`rush_insert`, `reschedule`) need a previous plan. The generator's
previous plans are random (their order often contradicts the job links, so rush insertion
answers `sequence_cycle`), so on the sweep seeds with seed % 5 == 1 and no previous plan the
request gets a realistic one: the baseline's verified plan of the same request minus one
job (the rush job, drawn from `np.random.default_rng((seed, 165))`), with a frozen zone up
to the plan's median start. These variants exercise the frozen zone against a plan that is
feasible by construction.

Method-specific properties, per seed of 0-199:

- the baseline never answers INFEASIBLE (it cannot prove infeasibility);
- every FEASIBLE or OPTIMAL answer of the baseline and of CP-SAT verifies;
- CP-SAT never calls a problem INFEASIBLE that the baseline solved, and an OPTIMAL CP-SAT
  answer is no worse than the baseline on the primary objective.

Across the seeds every `Feature` and every override kind appears, enough requests carry a
frozen zone, and enough answers verify that the properties are not vacuous. Every failure
message names the seed.
"""

from __future__ import annotations

import functools
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pytest
from generators import OVERRIDE_KINDS, random_request

from twinflow.scheduling.baseline import CAPABILITIES as BASELINE_CAPABILITIES
from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.features import problem_features
from twinflow.scheduling.model import (
    CompiledProblem,
    ExcludeMachineOverride,
    HoldOverride,
    LockOverride,
    Operation,
    Override,
    PreferMachineOverride,
    PreviousAssignment,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.objectives import primary_kind
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.registry import SolverInfo, available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, OptionalDependencyError, ParamValue, Solver
from twinflow.scheduling.solver import SOLUTION_STATUSES, solve
from twinflow.scheduling.validator import validate

SEEDS = range(200)
LONG_SEEDS = range(200, 1500)
DETERMINISM_SEEDS = range(0, 200, 10)
PROPERTY_BUDGET_SECONDS = 1.0
# Per-solver budgets for this suite: CP-SAT is anytime and would spend its whole budget on
# the seeds it cannot close; 0.25 s keeps the 1500-seed sweep bounded (T-165).
SUITE_BUDGET_SECONDS: dict[str, float] = {"ortools": 0.25}
REGISTERED = available()
# Construction params per solver for this suite (T-128).
SUITE_PARAMS: dict[str, dict[str, ParamValue]] = {"ortools": {"workers": 1, "seed": 0}}
# Method params (`profile.method_params[name]`) per solver for this suite: improvement methods
# are anytime, so an iteration limit keeps the sweep well inside the suite's runtime (T-147,
# T-148); their defaults (sa: 20,000 iterations) would run to the 1 s budget on every seed.
SUITE_METHOD_PARAMS: dict[str, dict[str, ParamValue]] = {
    "tabu": {"iteration_limit": 20},
    "sa": {"sa_iterations": 200},
}
REACTIVE_MODULUS = 5
REACTIVE_RESIDUE = 1
REACTIVE_STREAM = 165
REACTIVE_TOLERANCES = (0.0, 30.0)
TOLERANCE = 1e-6
MIN_BASELINE_VERIFIED = 150
MIN_CPSAT_VERIFIED = 50
MIN_FROZEN_CASES = 100
# Verified answers with a non-empty frozen set the long sweep must reach per reactive method,
# so the previous-plan path is tested, not just refused with `previous_plan_required`.
MIN_REACTIVE_FROZEN_VERIFIED = {"rush_insert": 150, "reschedule": 250}
_UNSUPPORTED = re.compile(r"does not support feature (\w+)$")


# --- sweep cases ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Case:
    """One sweep request with what the checks need, compiled once per seed."""

    seed: int
    request: SolveRequest
    compiled_issues: tuple[ScheduleIssue, ...]
    features: frozenset[Feature]
    frozen: tuple[PreviousAssignment, ...]


@functools.cache
def _request(seed: int) -> SolveRequest:
    return random_request(seed)


@functools.cache
def _case(seed: int) -> _Case:
    request = _sweep_request(seed)
    compiled = apply_overrides(request)
    return _Case(
        seed=seed,
        request=request,
        compiled_issues=tuple(validate(compiled)),
        features=problem_features(compiled.problem),
        frozen=_expected_frozen(request, compiled),
    )


def _sweep_request(seed: int) -> SolveRequest:
    """The generator's request, or its reactive variant on every fifth seed without a plan."""
    base = _request(seed)
    if seed % REACTIVE_MODULUS != REACTIVE_RESIDUE or base.previous_plan is not None:
        return base
    return _reactive_variant(seed, base) or base


def _reactive_variant(seed: int, base: SolveRequest) -> SolveRequest | None:
    """`base` with the baseline's plan minus one rush job as previous plan, and a frozen zone."""
    plan = solve(base, solver=BaselineSolver())
    if not plan.verified:
        return None
    compiled = apply_overrides(base).problem
    job_of = {op.id: job.id for job in compiled.jobs for op in job.operations}
    pending = {op.id for job in compiled.jobs for op in job.operations if _pending(op)}
    candidates = sorted({job_of[op_id] for op_id in pending})
    if not candidates:
        return None
    rng = np.random.default_rng((seed, REACTIVE_STREAM))
    rush = candidates[int(rng.integers(len(candidates)))]
    kept = [
        PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
        for a in plan.schedule.assignments
        if a.operation_id in pending and job_of[a.operation_id] != rush
    ]
    if not kept:
        return None
    data = base.model_dump(mode="json")
    data["previous_plan"] = {
        "plan_id": "prev",
        "problem_digest": "digest",
        "assignments": [a.model_dump(mode="json") for a in kept],
    }
    data["profile"]["frozen_zone"] = {
        "until": statistics.median(a.start for a in kept),
        "tolerance": REACTIVE_TOLERANCES[int(rng.integers(len(REACTIVE_TOLERANCES)))],
    }
    return SolveRequest.model_validate(data)


def _pending(op: Operation) -> bool:
    return op.status.state == "pending"


# --- the frozen set, from the raw request (TF-002 DEV-16) -------------------------------------


def _expected_frozen(
    request: SolveRequest, compiled: CompiledProblem
) -> tuple[PreviousAssignment, ...]:
    """Previous assignments the frozen zone fixes, by DEV-16, independent of the verifier."""
    zone, plan = request.profile.frozen_zone, request.previous_plan
    if zone is None or plan is None:
        return ()
    jobs = {op.id: job for job in compiled.problem.jobs for op in job.operations}
    states = {op.id: op.status.state for job in compiled.problem.jobs for op in job.operations}
    held = set(compiled.held_job_ids) | {
        o.job_id for o in request.overrides if isinstance(o, HoldOverride) and o.until is None
    }
    parents = {child: parent for parent, kids in compiled.split_map.items() for child in kids}
    frozen = []
    for previous in plan.assignments:
        job = jobs.get(previous.operation_id)
        if previous.start >= zone.until or job is None:
            continue
        if states[previous.operation_id] != "pending" or job.status != "open" or job.id in held:
            continue
        job_ids = {job.id, parents.get(job.id, job.id)}
        if not any(_releases(o, previous, job_ids) for o in request.overrides):
            frozen.append(previous)
    return tuple(frozen)


def _releases(override: Override, previous: PreviousAssignment, job_ids: set[str]) -> bool:
    """A raw hard lock on the op, exclude of its machine or prefer of another one wins."""
    if override.hardness != "hard":
        return False
    if isinstance(override, LockOverride):
        return override.operation_id == previous.operation_id
    if isinstance(override, ExcludeMachineOverride | PreferMachineOverride):
        targeted = override.operation_id == previous.operation_id or override.job_id in job_ids
        excluded = isinstance(override, ExcludeMachineOverride)
        return targeted and (override.machine_id == previous.machine_id) == excluded
    return False


def _frozen_failures(case: _Case, result: SolveResult) -> list[str]:
    """Frozen ops that left their previous machine or moved beyond the tolerance."""
    zone = case.request.profile.frozen_zone
    if zone is None or result.schedule.status not in SOLUTION_STATUSES:
        return []
    by_op = {a.operation_id: a for a in result.schedule.assignments}
    failures = []
    for previous in case.frozen:
        a = by_op.get(previous.operation_id)
        where = None if a is None else (a.machine_id, a.start)
        if where is None or not _kept(previous, where, zone.tolerance):
            failures.append(
                f"frozen {previous.operation_id} at {previous.machine_id} {previous.start:g} "
                f"+- {zone.tolerance:g}, got {where}"
            )
    return failures


def _kept(previous: PreviousAssignment, where: tuple[str, float], tolerance: float) -> bool:
    machine_id, start = where
    return (
        machine_id == previous.machine_id and abs(start - previous.start) <= tolerance + TOLERANCE
    )


# --- running and checking --------------------------------------------------------------------


def _solver(name: str) -> Solver:
    """The registered solver `name`; skips when its optional dependency is absent."""
    try:
        return get_solver(name, **SUITE_PARAMS.get(name, {}))
    except OptionalDependencyError as error:
        pytest.skip(f"{name}: {error}")


def _run(name: str, solver: Solver, request: SolveRequest) -> SolveResult:
    request = _with_method_params(request, name)
    budget = SUITE_BUDGET_SECONDS.get(name, PROPERTY_BUDGET_SECONDS)
    return solve(request, solver=solver, budget_seconds=budget)


@functools.cache
def _registered(name: str, seed: int) -> SolveResult:
    """The registered solver `name` on the sweep request of seed `seed` (cached, seeds 0-199)."""
    return _run(name, _solver(name), _case(seed).request)


def _with_method_params(request: SolveRequest, name: str) -> SolveRequest:
    params = SUITE_METHOD_PARAMS.get(name)
    if params is None:
        return request
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = data["profile"].get("method_params", {}) | {name: params}
    return SolveRequest.model_validate(data)


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


def _contract_failures(
    case: _Case, result: SolveResult, capabilities: frozenset[Feature]
) -> list[str]:
    """Every way `result` breaks the solver contract on `case`; empty when it keeps it."""
    if "solver_error" in _issue_codes(result):
        return ["raised (solver_error)"]
    status = result.schedule.status
    if status not in SOLUTION_STATUSES and not result.issues:
        return [f"{status} without a named issue"]
    if case.compiled_issues:
        refused = result.issues == case.compiled_issues and status == "UNKNOWN"
        return [] if refused and not result.verified else ["compiled issues not passed through"]
    missing = {f.value for f in case.features - capabilities}
    failures = _refusal_failures(result, missing)
    if status in SOLUTION_STATUSES:
        if not result.verified:
            failures.append(f"{status} did not verify")
        elif not result.objective_values:
            failures.append(f"{status} without objective values")
    return failures + _frozen_failures(case, result)


def _refusal_failures(result: SolveResult, missing: set[str]) -> list[str]:
    if _named_unsupported(result) != missing:
        return [f"refusal names {sorted(_named_unsupported(result))}, missing {sorted(missing)}"]
    refused = result.schedule.status == "UNKNOWN" and not result.schedule.assignments
    if missing and not (refused and not result.verified):
        return ["refused features but answered"]
    return []


def _assert_contract(seed: int, result: SolveResult, capabilities: frozenset[Feature]) -> None:
    failures = _contract_failures(_case(seed), result, capabilities)
    assert not failures, f"seed {seed} ({result.schedule.solver}): {failures}: {result.issues}"


def _outcome(case: _Case, result: SolveResult) -> str:
    """Tally key: verified (with or without frozen ops) or status plus issue codes."""
    if result.verified:
        return "verified+frozen" if case.frozen else "verified"
    codes = ",".join(sorted(set(_issue_codes(result))))
    return f"{result.schedule.status}:{codes}"


def _sweep(info: SolverInfo, seeds: Iterable[int]) -> tuple[list[str], Counter[str]]:
    """Run `info` on every seed; the contract failures (one line per seed) and the tally."""
    solver = _solver(info.name)
    failures: list[str] = []
    tally: Counter[str] = Counter()
    for seed in seeds:
        case = _case(seed)
        result = _run(info.name, solver, case.request)
        tally[_outcome(case, result)] += 1
        broken = _contract_failures(case, result, info.capabilities)
        if broken:
            failures.append(f"seed {seed}: {broken}: issues {_issue_codes(result)}")
    return failures, tally


def _sweep_message(info: SolverInfo, failures: Sequence[str], tally: Counter[str]) -> str:
    first_seed = failures[0].split(":")[0].removeprefix("seed ")
    repro = (
        ".venv/bin/python -c \"import sys; sys.path.insert(0, 'tests/scheduling'); "
        f"import test_properties as t; print(t.repro('{info.name}', {first_seed}))\""
    )
    return (
        f"{info.name}: {len(failures)} seeds break the contract; first {failures[0]}; "
        f"repro: {repro}; tally {dict(tally)}"
    )


def repro(name: str, seed: int) -> list[str]:
    """One sweep case by hand: the contract failures and issues of solver `name` on `seed`."""
    case = _case(seed)
    solver = get_solver(name, **SUITE_PARAMS.get(name, {}))
    result = _run(name, solver, case.request)
    failures = _contract_failures(case, result, solver.capabilities)
    return [*failures, *(f"{i.code} {i.path}: {i.message}" for i in result.issues)]


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


@pytest.mark.slow
def test_sweep_requests_are_valid_and_carry_enough_frozen_zones() -> None:
    seeds = [*SEEDS, *LONG_SEEDS]
    invalid = [
        seed for seed in seeds if validate(_case(seed).request) or _case(seed).compiled_issues
    ]
    frozen = [seed for seed in seeds if _case(seed).frozen]
    variants = [seed for seed in seeds if _case(seed).request is not _request(seed)]

    assert not invalid, f"invalid sweep requests: seeds {invalid[:10]}"
    assert len(frozen) >= MIN_FROZEN_CASES, f"only {len(frozen)} seeds have frozen ops"
    assert len(variants) >= MIN_FROZEN_CASES, f"only {len(variants)} reactive variants"


def test_reactive_variant_keeps_a_frozen_set_and_drops_a_rush_job() -> None:
    seed = next(s for s in SEEDS if _case(s).request is not _request(s))
    case = _case(seed)
    plan = case.request.previous_plan
    pending = {op.id for job in case.request.problem.jobs for op in job.operations if _pending(op)}

    assert plan is not None and case.request.profile.frozen_zone is not None, f"seed {seed}"
    assert case.frozen, f"seed {seed}: the variant freezes nothing"
    assert pending - {a.operation_id for a in plan.assignments}, f"seed {seed}: no rush op"


# --- every registered solver ---------------------------------------------------------------


@pytest.mark.parametrize("info", REGISTERED, ids=lambda info: info.name)
def test_every_registered_solver_keeps_the_contract(info: SolverInfo) -> None:
    failures, tally = _sweep(info, SEEDS)

    assert not failures, _sweep_message(info, failures, tally)


@pytest.mark.slow
@pytest.mark.parametrize("info", REGISTERED, ids=lambda info: info.name)
def test_every_registered_solver_keeps_the_contract_on_the_long_sweep(info: SolverInfo) -> None:
    failures, tally = _sweep(info, LONG_SEEDS)

    assert not failures, _sweep_message(info, failures, tally)
    floor = MIN_REACTIVE_FROZEN_VERIFIED.get(info.name, 0)
    assert tally["verified+frozen"] >= floor, f"{info.name}: tally {dict(tally)}"


def test_the_suite_covers_the_whole_registry() -> None:
    assert {info.name for info in REGISTERED} >= {"baseline", "ortools"}
    assert [info.name for info in REGISTERED] == [info.name for info in available()]
    assert set(MIN_REACTIVE_FROZEN_VERIFIED) <= {info.name for info in REGISTERED}


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
        kind = primary_kind(_case(seed).request.profile.objective)
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
