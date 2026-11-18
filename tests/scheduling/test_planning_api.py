"""Planning functions for TF-005 (T-162; COMP-029, REQ-126; TF-005 SDD "TF-003 (dependency)").

`twinflow.scheduling.portfolio.api`, re-exported by `twinflow.scheduling` (with the
`PortfolioResult`, `Candidate` and `MenuEntry` types TF-005 stores), pure (only
`time.monotonic`), Pydantic in and out. Signatures as the TF-005 planning service calls them:

- `solve_portfolio(request, *, budget_seconds=None, seed=0, pipelines=None, k_lookup=None)
  -> PortfolioResult` (COMP-024).
- `get_pareto_menu(result, axes=None) -> tuple[MenuEntry, ...]`: None or the menu's own axes
  return `result.menu`; other axes recompute `pareto.menu` from the verified candidates'
  stored values (no solver runs); axes must be 2 or 3 distinct kinds (`MenuAxes`), else
  ValueError.
- `resolve_with_overrides(request, previous_plan, overrides, *, budget_seconds=None, seed=0)
  -> PortfolioResult`: the request with `previous_plan` set and `overrides` appended to its
  own; pipelines `reschedule` first (index 00), then the profile portfolio (01, 02, ...);
  `effective_objective` carries churn as `reschedule` does (weighted sum: a churn term with
  `weight = profile.churn_weight`, `scale = len(previous_plan.assignments)`; lexicographic: a
  last churn level).
- `insert_rush_order(request, previous_plan, rush_job, *, budget_seconds=None, seed=0)
  -> SolveResult`: `rush_job` appended to the problem, solved by `rush_insert` with the
  previous plan; relaxations in `result.schedule.relaxations`.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from bench.hmlv import HELD_OUT_SEEDS, hmlv_instance
from kernel_cases import machine, op, request_from

import twinflow.scheduling as facade
from twinflow.scheduling import registry
from twinflow.scheduling.model import (
    Job,
    Lexicographic,
    LockOverride,
    ObjectiveKind,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    SchedulingProfile,
    SolveRequest,
    SolveResult,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.objectives import rank
from twinflow.scheduling.portfolio import pareto
from twinflow.scheduling.solver import solve

API = "twinflow.scheduling.portfolio.api"
PORTFOLIO_SRC = Path(__file__).resolve().parents[2] / "src/twinflow/scheduling/portfolio"
SEED = HELD_OUT_SEEDS[2]
WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP
CHURN = ObjectiveKind.CHURN
MAKESPAN = ObjectiveKind.MAKESPAN
WT_SUM = WeightedSum(terms=(WeightedTerm(kind=WT, weight=1.0),))
WT_LEX = {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
FUNCTIONS = ("solve_portfolio", "get_pareto_menu", "resolve_with_overrides", "insert_rush_order")


def _api() -> ModuleType:
    return importlib.import_module(API)


def _codes(issues: Any) -> list[str]:
    return [issue.code for issue in issues]


def _small(**profile_fields: Any) -> SolveRequest:
    base = hmlv_instance(SEED, jobs=12, ops_per_job=3, calendars=False)
    fields = {"objective": WT_SUM} | profile_fields
    profile = SchedulingProfile.model_validate(base.profile.model_dump() | fields)
    return base.model_copy(update={"profile": profile})


def _plan_of(schedule: Schedule, plan_id: str = "previous") -> PreviousPlan:
    return PreviousPlan(
        plan_id=plan_id,
        problem_digest="previous-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=a.operation_id, machine_id=a.machine_id, start=a.start)
            for a in schedule.assignments
        ),
    )


# --- facade exports and signatures -------------------------------------------------------------


@pytest.mark.parametrize("name", FUNCTIONS)
def test_planning_function_is_exported_by_the_facade(name: str) -> None:
    api = _api()
    assert getattr(facade, name) is getattr(api, name)
    assert name in facade.__all__


@pytest.mark.parametrize("name", ["PortfolioResult", "Candidate", "MenuEntry"])
def test_portfolio_types_are_exported_by_the_facade(name: str) -> None:
    from twinflow.scheduling import model

    assert getattr(facade, name) is getattr(model, name)
    assert name in facade.__all__


@pytest.mark.parametrize(
    ("name", "positional", "keywords"),
    [
        (
            "solve_portfolio",
            ["request"],
            {"budget_seconds": None, "seed": 0, "pipelines": None, "k_lookup": None},
        ),
        ("get_pareto_menu", ["result", "axes"], {}),
        (
            "resolve_with_overrides",
            ["request", "previous_plan", "overrides"],
            {"budget_seconds": None, "seed": 0},
        ),
        (
            "insert_rush_order",
            ["request", "previous_plan", "rush_job"],
            {"budget_seconds": None, "seed": 0},
        ),
    ],
)
def test_signatures_match_the_tf005_planning_service_calls(
    name: str, positional: list[str], keywords: dict[str, Any]
) -> None:
    parameters = inspect.signature(getattr(_api(), name)).parameters
    plain = [p for p in parameters.values() if p.kind is not inspect.Parameter.KEYWORD_ONLY]
    assert [p.name for p in plain] == positional
    keyword_only = {
        p.name: p.default for p in parameters.values() if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert keyword_only == keywords
    if name == "get_pareto_menu":
        assert parameters["axes"].default is None


# --- get_pareto_menu ---------------------------------------------------------------------------


@functools.cache
def _portfolio() -> Any:
    return _api().solve_portfolio(_small(), budget_seconds=4.0, pipelines=("edd", "atcs", "atc"))


def _verified_values(result: Any) -> dict[str, Any]:
    return {c.id: c.objective_values for c in result.candidates if c.verified}


def test_get_pareto_menu_without_axes_returns_the_result_menu() -> None:
    result = _portfolio()
    assert result.menu
    assert _api().get_pareto_menu(result) == result.menu


def test_get_pareto_menu_with_the_profile_axes_returns_the_result_menu() -> None:
    result = _portfolio()
    assert _api().get_pareto_menu(result, _small().profile.menu_axes) == result.menu


@pytest.mark.parametrize("axes", [(MAKESPAN, SETUP), (WT, MAKESPAN, SETUP), (SETUP, WT)])
def test_get_pareto_menu_with_other_axes_recomputes_from_stored_values(
    axes: tuple[ObjectiveKind, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _portfolio()

    def no_solver_runs() -> Any:
        raise AssertionError("get_pareto_menu must not look up or run a solver")

    monkeypatch.setattr(registry, "_entries", no_solver_runs)
    menu = _api().get_pareto_menu(result, axes)
    assert menu == pareto.menu(_verified_values(result), axes)
    assert all(tuple(entry.values) == axes for entry in menu)


def test_get_pareto_menu_works_on_a_result_read_back_from_json() -> None:
    result = _portfolio()
    stored = type(result).model_validate_json(result.model_dump_json())
    axes = (MAKESPAN, SETUP)
    assert _api().get_pareto_menu(stored, axes) == _api().get_pareto_menu(result, axes)


@pytest.mark.parametrize("axes", [(WT,), (WT, WT), (WT, SETUP, MAKESPAN, CHURN)])
def test_get_pareto_menu_rejects_axes_that_are_not_2_or_3_distinct_kinds(
    axes: tuple[ObjectiveKind, ...],
) -> None:
    with pytest.raises(ValueError):
        _api().get_pareto_menu(_portfolio(), axes)


# --- resolve_with_overrides --------------------------------------------------------------------


def _lock(lock_id: str, operation_id: str, machine_id: str, start: float) -> LockOverride:
    return LockOverride(
        id=lock_id,
        fact_ref="test",
        reason="operator pinned it",
        hardness="hard",
        operation_id=operation_id,
        machine_id=machine_id,
        start=start,
    )


@functools.cache
def _resolve_case(
    lexicographic: bool = False,
) -> tuple[SolveRequest, PreviousPlan, Any, dict[str, Any]]:
    """A small instance planned by `atcs`; the last ops of J0 and J1 are then locked late.

    J0's lock is already in the request, J1's arrives as the new override. Both lock starts
    are far after the previous plan, so every verified candidate moved both ops (churn >= 2)
    and only a solve that saw both overrides places them there.
    """
    objective: Any = Lexicographic.model_validate(WT_LEX) if lexicographic else WT_SUM
    base = _small(objective=objective, default_portfolio=("edd", "atcs"), churn_weight=2.0)
    previous = solve(base, solver=registry.get("atcs"), budget_seconds=10.0)
    assert previous.verified, previous.issues
    plan = _plan_of(previous.schedule)
    horizon = max(a.end for a in previous.schedule.assignments)
    at = {a.operation_id: a for a in previous.schedule.assignments}
    last0, last1 = base.problem.jobs[0].operations[-1].id, base.problem.jobs[1].operations[-1].id
    locks = {
        last0: (at[last0].machine_id, horizon + 5_000.0),
        last1: (at[last1].machine_id, horizon + 10_000.0),
    }
    existing = _lock("LOCK-0", last0, *locks[last0])
    request = base.model_copy(update={"overrides": (existing,)})
    new = (_lock("LOCK-1", last1, *locks[last1]),)
    result = _api().resolve_with_overrides(request, plan, new, budget_seconds=8.0)
    return request, plan, result, locks


def test_resolve_runs_reschedule_first_then_the_profile_portfolio() -> None:
    _, _, result, _ = _resolve_case()
    pipeline_ids = [c.id for c in result.candidates if not c.id.startswith("pareto:")]
    assert pipeline_ids == ["00:reschedule", "01:edd", "02:atcs"]
    assert result.candidates[0].pipeline == ("reschedule",)


def test_resolve_reschedule_candidate_verifies_with_the_previous_plan() -> None:
    _, _, result, _ = _resolve_case()
    first = result.candidates[0]
    assert first.verified, first.issues
    assert "previous_plan_required" not in _codes(first.issues)


def test_resolve_applies_both_the_request_overrides_and_the_new_ones() -> None:
    _, _, result, locks = _resolve_case()
    verified = [c for c in result.candidates if c.verified]
    assert verified
    for candidate in verified:
        at = {a.operation_id: a for a in candidate.schedule.assignments}
        for operation_id, (machine_id, start) in locks.items():
            assert at[operation_id].machine_id == machine_id, candidate.id
            assert at[operation_id].start == pytest.approx(start), candidate.id


def test_resolve_evaluates_churn_against_the_previous_plan() -> None:
    _, _, result, _ = _resolve_case()
    for candidate in (c for c in result.candidates if c.verified):
        assert candidate.objective_values[CHURN] >= 2.0, candidate.id


def test_resolve_weighted_sum_effective_objective_carries_the_churn_term() -> None:
    _, plan, result, _ = _resolve_case()
    spec = result.effective_objective
    assert isinstance(spec, WeightedSum)
    churn = [t for t in spec.terms if t.kind is CHURN]
    assert churn == [WeightedTerm(kind=CHURN, weight=2.0, scale=float(len(plan.assignments)))]
    assert spec.terms[0] == WeightedTerm(kind=WT, weight=1.0)


def test_resolve_lexicographic_effective_objective_ends_with_a_churn_level() -> None:
    _, _, result, _ = _resolve_case(lexicographic=True)
    spec = result.effective_objective
    assert isinstance(spec, Lexicographic)
    assert [level.kind for level in spec.levels] == [WT, CHURN]


def test_resolve_recommendation_ranks_under_the_churn_objective() -> None:
    _, _, result, _ = _resolve_case()
    values = _verified_values(result)
    first = next(c.id for c in result.candidates if c.verified)
    assert result.recommended == rank(values, result.effective_objective, reference=first)[0]


def test_resolve_does_not_mutate_its_inputs() -> None:
    request, plan, _, _ = _resolve_case()
    assert request.previous_plan is None
    assert [o.id for o in request.overrides] == ["LOCK-0"]
    assert plan.plan_id == "previous"


# --- insert_rush_order -------------------------------------------------------------------------
#
# The T-154 shop (tests/scheduling/test_reactive.py): two machines with colour changeovers; the
# green class-4 rush job R (due 60) goes on M2 between E and F, from 20 (setup to 25) to 35.

SHOP_PREVIOUS = {
    "A-1": ("M1", 0.0),
    "B-1": ("M1", 10.0),
    "C-1": ("M1", 20.0),
    "D-1": ("M2", 0.0),
    "E-1": ("M2", 10.0),
    "F-1": ("M2", 20.0),
}


def _shop_job(job_id: str, color: str, machines: Sequence[str], due: float, **weight: Any) -> Any:
    operation = op(f"{job_id}-1", dict.fromkeys(machines, 10.0), setup_params={"color": color})
    return {"id": job_id, "due": due, "operations": [operation]} | weight


def _shop() -> tuple[SolveRequest, PreviousPlan, Job]:
    jobs = [
        _shop_job("A", "red", ["M1"], 10.0, weight_override=6.0),
        _shop_job("B", "red", ["M1"], 100.0, weight_class=2),
        _shop_job("C", "blue", ["M1"], 35.0, weight_override=6.0),
        _shop_job("D", "blue", ["M2"], 10.0, weight_override=6.0),
        _shop_job("E", "blue", ["M2"], 100.0, weight_class=1),
        _shop_job("F", "red", ["M2"], 100.0, weight_class=1),
    ]
    problem = {
        "id": "rush-shop",
        "jobs": jobs,
        "machines": [machine("M1"), machine("M2")],
        "work_centers": [{"id": "WC", "setup_function_id": "S"}],
        "setup_functions": [
            {
                "id": "S",
                "terms": [{"kind": "mismatch", "param": "color", "seconds": 5.0}],
                "default_seconds": 0.0,
            }
        ],
    }
    plan = PreviousPlan(
        plan_id="shop-previous",
        problem_digest="shop-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=o, machine_id=m, start=s)
            for o, (m, s) in SHOP_PREVIOUS.items()
        ),
    )
    rush = Job.model_validate(_shop_job("R", "green", ["M1", "M2"], 60.0, weight_class=4))
    return request_from(problem, profile={"objective": WT_LEX}), plan, rush


def _single_machine() -> tuple[SolveRequest, PreviousPlan, Job]:
    """T-155: P 0-10 (w 6, due 10), Q 10-20 (w 5, due 20), X 20-30; rush R run 1, due 5.

    Q is relaxed 20 -> 21, then P 10 -> 11, and R goes first (test_reactive pins the trace).
    """
    rows = [("JOB-P", 10.0, 10.0, 6.0), ("JOB-Q", 10.0, 20.0, 5.0), ("JOB-X", 10.0, 100.0, 1.0)]
    jobs = [
        {"id": j, "due": due, "weight_override": w, "operations": [op(f"{j}-1", {"M1": run})]}
        for j, run, due, w in rows
    ]
    problem = {
        "id": "relax",
        "jobs": jobs,
        "machines": [machine("M1")],
        "work_centers": [{"id": "WC"}],
    }
    plan = PreviousPlan(
        plan_id="relax-previous",
        problem_digest="relax-digest",
        assignments=tuple(
            PreviousAssignment(operation_id=f"{j}-1", machine_id="M1", start=10.0 * k)
            for k, (j, *_) in enumerate(rows)
        ),
    )
    rush = Job.model_validate(
        {"id": "JOB-R", "due": 5.0, "weight_class": 4, "operations": [op("JOB-R-1", {"M1": 1.0})]}
    )
    return request_from(problem, profile={"objective": WT_LEX}), plan, rush


def test_insert_rush_order_appends_the_job_and_solves_with_rush_insert() -> None:
    request, plan, rush = _shop()
    result = _api().insert_rush_order(request, plan, rush, budget_seconds=10.0)
    assert isinstance(result, SolveResult)
    assert result.verified, result.issues
    assert result.schedule.solver == "rush_insert"
    at = {a.operation_id: a for a in result.schedule.assignments}
    assert set(at) == {*SHOP_PREVIOUS, "R-1"}
    assert (at["R-1"].machine_id, at["R-1"].start, at["R-1"].end) == ("M2", 20.0, 35.0)
    assert result.schedule.relaxations == ()


def test_insert_rush_order_returns_the_relaxations() -> None:
    request, plan, rush = _single_machine()
    result = _api().insert_rush_order(request, plan, rush, budget_seconds=10.0)
    assert result.verified, result.issues
    relaxed = [(r.job_id, r.old_due, r.new_due) for r in result.schedule.relaxations]
    assert relaxed == [("JOB-Q", 20.0, 21.0), ("JOB-P", 10.0, 11.0)]
    assert _codes(result.issues).count("relaxed_due") == 2


def test_insert_rush_order_does_not_mutate_the_request() -> None:
    request, plan, rush = _shop()
    _api().insert_rush_order(request, plan, rush, budget_seconds=10.0)
    assert [j.id for j in request.problem.jobs] == ["A", "B", "C", "D", "E", "F"]
    assert request.previous_plan is None


def test_insert_rush_order_with_a_duplicate_job_id_is_refused_not_verified() -> None:
    request, plan, _ = _shop()
    duplicate = request.problem.jobs[0]
    result = _api().insert_rush_order(request, plan, duplicate, budget_seconds=10.0)
    assert not result.verified
    assert result.issues


# --- JSON round trips --------------------------------------------------------------------------


def test_insert_rush_order_inputs_and_output_round_trip_through_json() -> None:
    request, plan, rush = _shop()
    request2 = SolveRequest.model_validate_json(request.model_dump_json())
    plan2 = PreviousPlan.model_validate_json(plan.model_dump_json())
    rush2 = Job.model_validate_json(rush.model_dump_json())
    assert (request2, plan2, rush2) == (request, plan, rush)
    result = _api().insert_rush_order(request2, plan2, rush2, budget_seconds=10.0)
    assert SolveResult.model_validate_json(result.model_dump_json()) == result


def test_portfolio_and_resolve_results_round_trip_through_json() -> None:
    for result in (_portfolio(), _resolve_case()[2]):
        again = type(result).model_validate_json(result.model_dump_json())
        assert again == result


# --- boundary ----------------------------------------------------------------------------------

FORBIDDEN_PACKAGES = (
    "twinflow.application",
    "twinflow.service",
    "twinflow.mcp",
    "twinflow.scheduling.algorithms",
)
IO_MODULES = frozenset(
    {"os", "pathlib", "socket", "urllib", "http", "httpx", "requests", "shutil", "subprocess",
     "io", "tempfile", "datetime"}
)  # fmt: skip


def _imports(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
            names += [f"{node.module}.{alias.name}" for alias in node.names]
    return names


@pytest.mark.parametrize("filename", ["api.py", "runner.py"])
def test_planning_modules_import_no_surface_no_algorithm_and_no_io(filename: str) -> None:
    tree = ast.parse((PORTFOLIO_SRC / filename).read_text(encoding="utf-8"))
    imported = _imports(tree)
    for name in imported:
        assert not any(name == p or name.startswith(p + ".") for p in FORBIDDEN_PACKAGES), name
        assert name.split(".")[0] not in IO_MODULES, name
    time_uses = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "time"
    }
    from_time = {n.split(".", 1)[1] for n in imported if n.startswith("time.")}
    assert time_uses | from_time <= {"monotonic"}, time_uses | from_time
    opens = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open"
    ]
    assert opens == []
