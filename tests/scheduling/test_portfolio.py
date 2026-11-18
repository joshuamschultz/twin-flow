"""Portfolio runner in process (T-160; COMP-024, REQ-115, REQ-118, REQ-121, D-079; SDD
"Portfolio" pseudo-code, Sec 17.3 p.463-466, Sec 18.3 p.492-495).

API these tests pin (`twinflow.scheduling.portfolio.runner`):

`solve_portfolio(request, *, budget_seconds=None, seed=0, pipelines=None, k_lookup=None)
-> PortfolioResult`. `pipelines` None means `profile.default_portfolio`; each entry is
"a->b->c" (registry names). With `profile.workers == 1` everything runs in this process.

- Every stage yields a candidate (SDD "every stage"; the spec leaves the id of a stage
  open, these tests pin it): stage k of pipeline index i has `pipeline` = the stage names up
  to and including k and `id` = f"{i:02d}:{'->'.join(pipeline)}". So `atcs->tabu` at index
  1 yields "01:atcs" (pipeline ("atcs",)) and "01:atcs->tabu" (pipeline ("atcs", "tabu")),
  in that order; pipeline candidates come first in (index, stage) order, then "pareto:{n}".
  A candidate's `schedule.solver` is the stage's name, `schedule.elapsed_seconds` its time.
- A stage receives the previous stage's schedule as `context.warm_start` when that schedule
  verified (spy: a wrapped registry entry).
- An unknown stage name gives an `unknown_solver` issue naming it (on the candidate or the
  result) and the other pipelines still run.
- Pareto search seeds from the verified dispatch candidates; archive points become
  "pareto:{n}" candidates. `menu` = `pareto.menu` over the verified candidates' values on
  `profile.menu_axes`.
- `effective_objective` = `load.effective_objective(profile, load_ratio)` with `load_ratio`
  = `load.load_ratio(apply_overrides(request))`; `recommended` =
  `objectives.rank({verified id: values}, effective_objective, reference=first verified id)[0]`;
  no verified candidate: `recommended` None and a `no_verified_candidate` result issue.
- `atcs_runs` holds every `AtcsRun` of every candidate schedule; `k_lookup` is passed to each
  `atcs` stage's constructor; `reschedule` is built with `churn_weight=profile.churn_weight`.
- Wall time <= budget * 1.1 + 1 s.

The registry is spied by replacing its cached `_entries()` (what `get` and `is_registered`
read at call time), so the spy works however the runner imports the registry.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import time
from collections.abc import Mapping
from types import ModuleType
from typing import Any

import pytest
from bench.hmlv import HELD_OUT_SEEDS, hmlv_instance

from twinflow.scheduling import registry
from twinflow.scheduling.model import (
    LoadWeights,
    ObjectiveKind,
    ObjectiveSpec,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    Schedule,
    SchedulingProfile,
    SolveRequest,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.objectives import rank
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.portfolio import load, pareto
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, SolveContext
from twinflow.scheduling.solver import solve

RUNNER = "twinflow.scheduling.portfolio.runner"
SEED = HELD_OUT_SEEDS[0]
TRIO = ("edd", "atcs->tabu", "sb_wt")
TRIO_IDS = ["00:edd", "01:atcs", "01:atcs->tabu", "02:sb_wt"]
TRIO_PIPELINES = [("edd",), ("atcs",), ("atcs", "tabu"), ("sb_wt",)]
WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP
STATUSES = {"OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"}
LOAD_WEIGHTS = LoadWeights(
    light=(WeightedTerm(kind=WT, weight=1.0), WeightedTerm(kind=SETUP, weight=0.1)),
    heavy=(WeightedTerm(kind=WT, weight=0.3), WeightedTerm(kind=SETUP, weight=1.0)),
)
TABLE_K = (2.0, 0.5)


def _runner() -> ModuleType:
    return importlib.import_module(RUNNER)


def _request(seed: int = SEED, **profile_fields: Any) -> SolveRequest:
    """A held-out HMLV instance with a weighted-sum objective and load-dependent weights."""
    base = hmlv_instance(seed)
    objective = WeightedSum(terms=LOAD_WEIGHTS.light)
    fields = {"objective": objective, "load_weights": LOAD_WEIGHTS} | profile_fields
    profile = SchedulingProfile.model_validate(base.profile.model_dump() | fields)
    return base.model_copy(update={"profile": profile})


def _table_k(tau: float, r: float, eta: float, m: int) -> tuple[float, float]:
    """A fixed K-table lookup (module level, so it pickles)."""
    return TABLE_K


# --- registry spies ------------------------------------------------------------------------


class _SpySolver:
    """Delegates to a real solver and records the context each call received."""

    def __init__(self, inner: Any, contexts: list[SolveContext]) -> None:
        self._inner = inner
        self._contexts = contexts

    @property
    def name(self) -> str:
        return str(self._inner.name)

    @property
    def capabilities(self) -> frozenset[Feature]:
        return frozenset(self._inner.capabilities)

    def solve(
        self, problem: Problem, *, objective: ObjectiveSpec, budget_seconds: float, context: Any
    ) -> Schedule:
        self._contexts.append(context)
        result: Schedule = self._inner.solve(
            problem, objective=objective, budget_seconds=budget_seconds, context=context
        )
        return result


@dataclasses.dataclass
class _Spy:
    """What a spied registry entry saw: factory kwargs per build and contexts per solve."""

    kwargs: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    contexts: list[SolveContext] = dataclasses.field(default_factory=list)


def _spied(spec: SolverSpec, spy: _Spy) -> SolverSpec:
    def factory(**kwargs: Any) -> Any:
        spy.kwargs.append(kwargs)
        return _SpySolver(spec.factory(**kwargs), spy.contexts)

    return dataclasses.replace(spec, factory=factory)


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    spies: Mapping[str, _Spy] = {},  # noqa: B006  # read only
    extra: Mapping[str, SolverSpec] = {},  # noqa: B006  # read only
) -> None:
    entries = dict(registry._entries())
    patched = {
        name: (_spied(spec, spies[name]) if name in spies else spec, module)
        for name, (spec, module) in entries.items()
    }
    patched |= {name: (spec, "tests.fake") for name, spec in extra.items()}
    monkeypatch.setattr(registry, "_entries", lambda: patched)


# --- one shared spied run of the trio --------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _TrioRun:
    result: Any
    tabu: _Spy
    elapsed: float


@functools.cache
def _trio_run() -> _TrioRun:
    runner = _runner()
    request = _request()
    tabu = _Spy()
    with pytest.MonkeyPatch.context() as monkeypatch:
        _patch_registry(monkeypatch, {"tabu": tabu})
        started = time.monotonic()
        result = runner.solve_portfolio(request, budget_seconds=5.0, pipelines=TRIO)
        elapsed = time.monotonic() - started
    return _TrioRun(result, tabu, elapsed)


def _pipeline_candidates(result: Any) -> list[Any]:
    return [c for c in result.candidates if not c.id.startswith("pareto:")]


def _verified_values(result: Any) -> dict[str, Mapping[ObjectiveKind, float]]:
    return {c.id: c.objective_values for c in result.candidates if c.verified}


def _codes(issues: Any) -> list[str]:
    return [issue.code for issue in issues]


# --- candidates per stage ----------------------------------------------------------------------


def test_every_stage_of_every_pipeline_yields_a_candidate_in_order() -> None:
    result = _trio_run().result
    candidates = _pipeline_candidates(result)
    assert [c.id for c in candidates] == TRIO_IDS
    assert [c.pipeline for c in candidates] == TRIO_PIPELINES
    assert [c.schedule.solver for c in candidates] == ["edd", "atcs", "tabu", "sb_wt"]


def test_pipeline_candidates_come_before_pareto_candidates() -> None:
    ids = [c.id for c in _trio_run().result.candidates]
    first_pareto = next((k for k, cid in enumerate(ids) if cid.startswith("pareto:")), len(ids))
    assert all(cid.startswith("pareto:") for cid in ids[first_pareto:]), ids


def test_each_candidate_carries_status_verification_values_issues_and_elapsed() -> None:
    for candidate in _pipeline_candidates(_trio_run().result):
        assert candidate.schedule.status in STATUSES
        assert candidate.verified, (candidate.id, candidate.issues)
        assert set(candidate.objective_values) == set(ObjectiveKind), candidate.id
        assert isinstance(candidate.issues, tuple)
        assert candidate.schedule.elapsed_seconds >= 0


def test_the_tabu_stage_is_warm_started_from_the_atcs_schedule() -> None:
    run = _trio_run()
    atcs = next(c for c in run.result.candidates if c.id == "01:atcs")
    assert len(run.tabu.contexts) == 1
    warm = run.tabu.contexts[0].warm_start
    assert warm is not None, "the tabu stage got no warm start"
    assert warm.assignments == atcs.schedule.assignments


def test_a_first_stage_gets_no_warm_start(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    edd = _Spy()
    _patch_registry(monkeypatch, {"edd": edd})
    runner.solve_portfolio(_request(), budget_seconds=3.0, pipelines=("edd",))
    assert [c.warm_start for c in edd.contexts] == [None]


def test_an_unverified_stage_does_not_warm_start_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # rush_insert without a previous plan is UNKNOWN `previous_plan_required`.
    runner = _runner()
    tabu = _Spy()
    _patch_registry(monkeypatch, {"tabu": tabu})
    result = runner.solve_portfolio(
        _request(), budget_seconds=4.0, pipelines=("rush_insert->tabu",)
    )
    first = next(c for c in result.candidates if c.id == "00:rush_insert")
    assert not first.verified
    assert [c.warm_start for c in tabu.contexts] == [None]


def test_pipelines_none_runs_the_profile_default_portfolio() -> None:
    request = _request(default_portfolio=("edd", "atc"))
    result = _runner().solve_portfolio(request, budget_seconds=3.0)
    assert [c.id for c in _pipeline_candidates(result)] == ["00:edd", "01:atc"]


def test_an_unknown_solver_name_is_an_issue_and_the_rest_still_run() -> None:
    result = _runner().solve_portfolio(
        _request(), budget_seconds=3.0, pipelines=("edd", "no_such_method", "atcs")
    )
    issues = [*result.issues, *(i for c in result.candidates for i in c.issues)]
    unknown = [i for i in issues if i.code == "unknown_solver"]
    assert unknown, _codes(issues)
    assert any("no_such_method" in i.message for i in unknown)
    verified = {c.id for c in result.candidates if c.verified}
    assert {"00:edd", "02:atcs"} <= verified
    assert "01:no_such_method" not in verified


# --- menu, objective and recommendation --------------------------------------------------------


def test_menu_is_the_pareto_menu_of_the_verified_candidates_over_the_profile_axes() -> None:
    result = _trio_run().result
    axes = _request().profile.menu_axes
    assert result.menu == pareto.menu(_verified_values(result), axes)
    assert result.menu, "verified candidates always give a non-empty menu"


def test_menu_entries_are_mutually_non_dominated() -> None:
    result = _trio_run().result
    axes = _request().profile.menu_axes
    points = [tuple(entry.values[k] for k in axes) for entry in result.menu]
    for a in points:
        for b in points:
            if a != b:
                assert not all(x <= y for x, y in zip(a, b, strict=True)), (a, b)


def test_menu_includes_pareto_search_candidates() -> None:
    result = _runner().solve_portfolio(_request(), budget_seconds=5.0, pipelines=("edd", "atcs"))
    pareto_ids = [c.id for c in result.candidates if c.id.startswith("pareto:")]
    assert pareto_ids, "Pareto search added no candidate"
    assert all(c.verified for c in result.candidates if c.id in pareto_ids)
    assert any(entry.candidate_id.startswith("pareto:") for entry in result.menu), [
        entry.candidate_id for entry in result.menu
    ]


def test_effective_objective_is_the_load_weighted_profile_objective() -> None:
    result = _trio_run().result
    request = _request()
    ratio = load.load_ratio(apply_overrides(request))
    assert result.load_ratio == pytest.approx(ratio)
    assert result.effective_objective == load.effective_objective(request.profile, ratio)


def test_recommended_is_the_first_ranked_verified_candidate() -> None:
    result = _trio_run().result
    values = _verified_values(result)
    first_verified = next(c.id for c in result.candidates if c.verified)
    expected = rank(values, result.effective_objective, reference=first_verified)[0]
    assert result.recommended == expected
    assert "no_verified_candidate" not in _codes(result.issues)


def test_no_verified_candidate_gives_no_recommendation_and_a_named_issue() -> None:
    result = _runner().solve_portfolio(_request(), budget_seconds=3.0, pipelines=("rush_insert",))
    assert result.recommended is None
    assert "no_verified_candidate" in _codes(result.issues)
    assert result.menu == ()
    only = next(c for c in result.candidates if c.id == "00:rush_insert")
    assert not only.verified
    assert "previous_plan_required" in _codes(only.issues)


# --- ATCS records, k_lookup, reschedule churn weight -------------------------------------------


def test_atcs_runs_are_collected_from_the_candidates() -> None:
    result = _trio_run().result
    atcs = next(c for c in result.candidates if c.id == "01:atcs")
    assert {r.work_center_id for r in atcs.schedule.atcs_runs} == {"C0", "C1", "C2", "C3"}
    for candidate in result.candidates:
        for run in candidate.schedule.atcs_runs:
            assert run in result.atcs_runs, (candidate.id, run)


def test_k_lookup_is_passed_to_every_atcs_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    atcs = _Spy()
    _patch_registry(monkeypatch, {"atcs": atcs})
    result = runner.solve_portfolio(
        _request(), budget_seconds=4.0, pipelines=("atcs", "edd->atcs"), k_lookup=_table_k
    )
    assert len(atcs.kwargs) == 2
    assert all(kwargs.get("k_lookup") is _table_k for kwargs in atcs.kwargs)
    assert result.atcs_runs
    assert {(r.source, r.K1, r.K2) for r in result.atcs_runs} == {("table", *TABLE_K)}


def _previous_plan(request: SolveRequest) -> PreviousPlan:
    previous = solve(request, solver=registry.get("atcs"), budget_seconds=10.0)
    assert previous.verified, previous.issues
    return PreviousPlan(
        plan_id="previous",
        problem_digest="previous-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
            for a in previous.schedule.assignments
        ),
    )


@pytest.mark.parametrize("churn_weight", [0.0, 3.5])
def test_reschedule_stage_is_built_with_the_profile_churn_weight(
    monkeypatch: pytest.MonkeyPatch, churn_weight: float
) -> None:
    runner = _runner()
    base = _request(churn_weight=churn_weight)
    request = base.model_copy(update={"previous_plan": _previous_plan(base)})
    reschedule = _Spy()
    _patch_registry(monkeypatch, {"reschedule": reschedule})
    result = runner.solve_portfolio(request, budget_seconds=4.0, pipelines=("reschedule",))
    assert [kwargs.get("churn_weight") for kwargs in reschedule.kwargs] == [churn_weight]
    assert next(c for c in result.candidates if c.id == "00:reschedule").verified


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", [0.0, -1.0, float("nan"), float("inf")])
def test_an_invalid_budget_is_a_budget_issue_not_a_crash(budget: float) -> None:
    result = _runner().solve_portfolio(_request(), budget_seconds=budget, pipelines=("edd",))
    assert "budget" in _codes(result.issues)
    assert result.recommended is None
    assert not any(c.verified for c in result.candidates)


def test_an_invalid_request_reports_its_issues_and_runs_no_solver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    data = _request().model_dump(mode="json")
    data["problem"]["jobs"][0]["operations"][0]["options"][0]["machine_id"] = "GHOST"
    request = SolveRequest.model_validate(data)
    edd = _Spy()
    _patch_registry(monkeypatch, {"edd": edd})
    result = runner.solve_portfolio(request, budget_seconds=3.0, pipelines=("edd",))
    assert result.issues, "the validator's issues must be reported"
    assert result.recommended is None
    assert edd.contexts == []


def test_an_infinite_load_ratio_still_gives_a_json_round_trippable_result() -> None:
    # Every machine is closed before the horizon, so the load ratio is inf (COMP-023). The
    # model refuses non-finite floats (D-060): the runner must not crash on it.
    runner = _runner()
    request = _request()
    data = request.model_dump(mode="json")
    data["problem"]["horizon_end"] = 100.0
    data["problem"]["calendars"] = [{"id": "LATE", "intervals": [[1000.0, 1.0e9]]}]
    for machine in data["problem"]["machines"]:
        machine["calendar_id"] = "LATE"
    request = SolveRequest.model_validate(data)
    assert load.load_ratio(apply_overrides(request)) == float("inf")
    result = runner.solve_portfolio(request, budget_seconds=3.0, pipelines=("edd",))
    assert type(result).model_validate_json(result.model_dump_json()) == result


# --- wall time ---------------------------------------------------------------------------------


def test_wall_time_within_budget_plus_ten_percent_plus_one_second_at_5_seconds() -> None:
    run = _trio_run()
    assert run.elapsed <= 5.0 * 1.1 + 1.0, run.elapsed
    assert run.result.elapsed_seconds <= 5.0 * 1.1 + 1.0


@pytest.mark.slow
def test_wall_time_within_budget_plus_ten_percent_plus_one_second_at_30_seconds() -> None:
    runner = _runner()
    started = time.monotonic()
    result = runner.solve_portfolio(_request(), budget_seconds=30.0, pipelines=TRIO)
    elapsed = time.monotonic() - started
    assert elapsed <= 30.0 * 1.1 + 1.0, elapsed
    assert any(c.verified for c in result.candidates)


# --- bounded pipelines (OWASP LLM10: unbounded consumption) ----------------------------------


def test_pipelines_beyond_the_cap_are_refused_with_too_many_pipelines_and_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _runner()
    edd = _Spy()
    _patch_registry(monkeypatch, {"edd": edd})
    result = runner.solve_portfolio(_request(), budget_seconds=5.0, pipelines=("edd",) * 66)
    assert _codes(result.issues).count("too_many_pipelines") == 1, _codes(result.issues)
    ids = [c.id for c in _pipeline_candidates(result)]
    assert ids == [f"{i:02d}:edd" for i in range(runner.MAX_PIPELINES)]
    assert len(edd.contexts) == runner.MAX_PIPELINES


def test_a_pipeline_longer_than_the_cap_is_refused_with_pipeline_too_long() -> None:
    runner = _runner()
    longest = runner.STAGE_SEPARATOR.join(["edd"] * runner.MAX_STAGES)
    too_long = runner.STAGE_SEPARATOR.join(["edd"] * (runner.MAX_STAGES + 1))
    result = runner.solve_portfolio(
        _request(), budget_seconds=5.0, pipelines=("edd", too_long, longest)
    )
    issues = [i for i in result.issues if i.code == "pipeline_too_long"]
    assert len(issues) == 1 and "1" in issues[0].message, _codes(result.issues)
    ids = [c.id for c in _pipeline_candidates(result)]
    assert not any(i.startswith("01:") for i in ids), ids
    assert ids[0] == "00:edd"
    assert sum(i.startswith("02:") for i in ids) == runner.MAX_STAGES
