"""Filtered beam search (T-150, COMP-019, REQ-110; SDD "Filtered beam search", Sec 15.1).

API these tests pin (the implementer builds to it):

- `twinflow.scheduling.algorithms.beam` registers `beam` (family `improvement`,
  `deterministic=True`). Params through `profile.method_params["beam"]`: `beam_width`
  (default 4), `filter_width` (default 12; 0 = filter off), `rule` (`atcs` default, or
  `atc`: the rollout rule) and `K1` (the rollout rule's K1). Invalid values are refused as
  UNKNOWN with an `invalid_method_param` issue (the ATCS convention).
- `beam_search(problem, objective, *, beam_width=4, filter_width=12, rule="atcs",
  K1=None, deadline=None) -> BeamResult`, a pure function on a compiled `Problem`:
  `filter_width` None or 0 = off; `deadline` is a `time.monotonic()` instant (None = none).
- `BeamResult(best, best_value, levels)`: `best` the incumbent as the kernel's
  `Dispatched` (choice, sequences, decode); `best_value` its primary objective value;
  `levels[k]` the nodes rolled out at depth k + 1, a tuple of `BeamNode(key, value, kept)`:
  `key` the op ids committed from the root to the node in commit order, `value` the
  primary value of the node's rollout, `kept` whether the node entered the next beam.
  Children dropped by the filter are not rolled out and do not appear in `levels`.
- The incumbent starts as the root rollout, always computed (the anytime answer, even past
  the deadline), so a deadline that has already passed returns
  the root rollout with no levels.

How the rollout rule is built (registry name or otherwise) is the implementer's choice; these
tests only see values, keys and the returned plan.

Ex 15.1.1 (fixture pinedo_14_3_4, 1 || sum w_j T_j; p = 10, 10, 13, 4; d = 4, 2, 1, 12;
w = 14, 12, 1, 12) with ATC K = 5 rollouts: (1,*) -> 1,4,2,3 = 408; (2,*) -> 2,4,1,3 = 436;
(3,*) -> 3,4,1,2 = 814 (the book prints 771; recomputation gives 814, SDD note);
(4,*) -> 4,1,2,3 = 440. Width 2 keeps (1,*) and (2,*); the answer is 1,4,2,3 = 408.
"""

from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any

import pytest
from bench.gates import beats_baselines, format_verdict
from bench.hmlv import HELD_OUT_SEEDS, scale_instance
from generators import random_request
from kernel_cases import compile_request, load_request

from twinflow.scheduling.model import ObjectiveKind, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.solver import solve

WT = ObjectiveKind.WEIGHTED_TARDINESS
BOOK_PARAMS: dict[str, Any] = {"beam_width": 2, "filter_width": 0, "rule": "atc", "K1": 5.0}
LEVEL_1 = {("1-1",): 408.0, ("2-1",): 436.0, ("3-1",): 814.0, ("4-1",): 440.0}
KEPT_1 = {("1-1",), ("2-1",)}
BEST_SEQUENCE = ("1-1", "4-1", "2-1", "3-1")
ROOT_ATC_SEQUENCE = ("4-1", "1-1", "2-1", "3-1")  # ATC K=5 from the root, T-131: 440
SCALE_BUDGET_SECONDS = 30.0


def _beam() -> ModuleType:
    """The leaf, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.algorithms.beam")


def _with_params(request: SolveRequest, params: dict[str, Any]) -> SolveRequest:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {"beam": params}
    return SolveRequest.model_validate(data)


def _solve(request: SolveRequest, params: dict[str, Any], **kwargs: Any) -> SolveResult:
    return solve(_with_params(request, params), solver=get_solver("beam"), **kwargs)


def _order(result: SolveResult, machine_id: str) -> tuple[str, ...]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return tuple(a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.operation_id)))


def _search_1434(**kwargs: Any) -> Any:
    request = load_request("pinedo_14_3_4")
    compiled = compile_request(request)
    return _beam().beam_search(compiled.problem, compiled.profile.objective, **kwargs)


def _book_search(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {"beam_width": 2, "filter_width": None, "rule": "atc", "K1": 5.0}
    return _search_1434(**(kwargs | overrides))


# --- registration ------------------------------------------------------------------------------


def test_beam_is_registered_improvement_deterministic() -> None:
    info = {i.name: i for i in available()}
    assert "beam" in info, sorted(info)
    entry = info["beam"]
    assert entry.family == "improvement"
    assert entry.deterministic is True
    assert entry.module == "twinflow.scheduling.algorithms.beam"
    assert get_solver("beam").name == "beam"


@pytest.mark.parametrize(
    "params",
    [{"rule": "edd"}, {"beam_width": 0}, {"filter_width": -1}, {"no_such_param": 1}],
    ids=["rule-edd", "width-0", "filter-negative", "unknown-param"],
)
def test_beam_invalid_params_are_refused_by_name(params: dict[str, Any]) -> None:
    result = _solve(load_request("pinedo_14_3_4"), params)
    assert result.schedule.status == "UNKNOWN"
    codes = [i.code for i in (*result.schedule.issues, *result.issues)]
    assert "invalid_method_param" in codes, codes


# --- Example 15.1.1 (REQ-110) -------------------------------------------------------------------


def test_beam_search_level_1_rollouts_match_example_15_1_1() -> None:
    result = _book_search()
    assert result.levels, "no level recorded"
    level_1 = {node.key: node.value for node in result.levels[0]}
    assert level_1 == pytest.approx(LEVEL_1)


def test_beam_search_keeps_nodes_1_and_2_at_level_1() -> None:
    result = _book_search()
    kept = {node.key for node in result.levels[0] if node.kept}
    assert kept == KEPT_1


def test_beam_search_never_keeps_more_than_the_beam_width() -> None:
    result = _book_search()
    for depth, level in enumerate(result.levels):
        assert sum(node.kept for node in level) <= 2, depth
        keys = [node.key for node in level]
        assert len(keys) == len(set(keys)), depth
        assert all(len(key) == depth + 1 for key in keys), depth


def test_beam_search_level_2_expands_only_the_kept_nodes() -> None:
    result = _book_search()
    assert len(result.levels) >= 2
    parents = {node.key[:1] for node in result.levels[1]}
    assert parents == KEPT_1
    assert len(result.levels[1]) == 6  # three open jobs under each kept node


def test_beam_search_returns_1_4_2_3_with_408() -> None:
    result = _book_search()
    assert result.best_value == pytest.approx(408.0)
    assert tuple(result.best.sequences["M1"]) == BEST_SEQUENCE


def test_beam_solver_on_example_15_1_1_gives_1_4_2_3_and_408() -> None:
    result = _solve(load_request("pinedo_14_3_4"), BOOK_PARAMS)
    assert result.schedule.status == "FEASIBLE", result.schedule.issues
    assert result.verified, result.issues
    assert _order(result, "M1") == BEST_SEQUENCE
    assert result.objective_values[WT] == pytest.approx(408.0)
    assert result.schedule.primary_value == pytest.approx(408.0)


def test_beam_solver_is_repeatable() -> None:
    request = load_request("pinedo_14_3_4")
    first = _solve(request, BOOK_PARAMS)
    second = _solve(request, BOOK_PARAMS, seed=7)
    assert first.schedule.assignments == second.schedule.assignments


# --- filter --------------------------------------------------------------------------------------


def test_beam_filter_drops_children_beyond_its_width_before_rollouts() -> None:
    unfiltered = {node.key for node in _book_search().levels[0]}
    filtered = _book_search(filter_width=2)
    level_1 = {node.key for node in filtered.levels[0]}
    assert len(level_1) == 2, level_1
    assert level_1 <= unfiltered
    # (3,*) is the crude-worst child by any reading: 13 s placed first.
    assert ("3-1",) not in level_1
    for level in filtered.levels:
        assert len(level) <= 2


@pytest.mark.parametrize("off", [None, 0])
def test_beam_filter_off_rolls_out_every_child(off: int | None) -> None:
    assert len(_book_search(filter_width=off).levels[0]) == 4


def test_beam_filter_wider_than_the_children_changes_nothing() -> None:
    wide, off = _book_search(filter_width=12), _book_search(filter_width=None)
    assert [[(n.key, n.value, n.kept) for n in level] for level in wide.levels] == [
        [(n.key, n.value, n.kept) for n in level] for level in off.levels
    ]
    assert wide.best_value == off.best_value


# --- anytime incumbent ----------------------------------------------------------------------------


def test_beam_search_past_deadline_returns_the_root_rollout() -> None:
    result = _book_search(deadline=time.monotonic() - 1.0)
    assert result.levels == ()
    assert result.best_value == pytest.approx(440.0)
    assert tuple(result.best.sequences["M1"]) == ROOT_ATC_SEQUENCE


def test_beam_solver_with_a_spent_budget_returns_the_root_rollout_verified() -> None:
    # 1 microsecond: the deadline has passed before the first level, so the answer is the
    # incumbent the search starts from (the root rollout), not a refusal.
    request = load_request("pinedo_14_3_4")
    result = _solve(request, BOOK_PARAMS, budget_seconds=1e-6)
    assert result.schedule.status == "FEASIBLE", result.schedule.issues
    assert result.verified, result.issues
    assert _order(result, "M1") == ROOT_ATC_SEQUENCE
    assert result.objective_values[WT] == pytest.approx(440.0)


def test_beam_incumbent_is_never_worse_than_its_root_rollout() -> None:
    full = _book_search()
    root = _book_search(deadline=time.monotonic() - 1.0)
    assert full.best_value <= root.best_value


# --- generated problems (adversarial) -------------------------------------------------------------


@pytest.mark.parametrize("seed", range(15))
def test_beam_verifies_or_names_its_issue_on_generated_problems(seed: int) -> None:
    result = _solve(random_request(seed), {"beam_width": 2, "filter_width": 4})
    codes = {i.code for i in (*result.schedule.issues, *result.issues)}
    assert "solver_error" not in codes, (seed, codes)
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified, (seed, result.issues)
    else:
        assert codes, seed


# --- gates (slow) ---------------------------------------------------------------------------------


@pytest.mark.slow
def test_beam_beats_baselines() -> None:
    verdict = beats_baselines("beam")
    print("\n" + format_verdict("beam", verdict, len(HELD_OUT_SEEDS)))
    assert verdict.passed, verdict.failures


@pytest.mark.slow
def test_beam_respects_its_30_second_budget_on_600x8() -> None:
    request = scale_instance(0)
    started = time.perf_counter()
    result = solve(request, solver=get_solver("beam"), budget_seconds=SCALE_BUDGET_SECONDS)
    elapsed = time.perf_counter() - started
    assert result.verified, (result.schedule.status, [i.code for i in result.issues])
    assert len(result.schedule.assignments) == 600 * 8
    assert elapsed < SCALE_BUDGET_SECONDS, f"beam took {elapsed:.2f} s"
