"""Tabu search and simulated annealing (T-147, T-148; COMP-018, REQ-108, REQ-109, D-076).

`twinflow.scheduling.algorithms.local_search` registers `tabu` (Pinedo Alg 14.3.3, p.382-384)
and `sa` (Alg 14.3.2, p.380-382), family `improvement`, every `Feature` (decoder-backed).
`tabu` is deterministic; `sa` is not (it draws from `random.Random(context.seed)`).

API these tests pin (the implementer builds to it):

- `Plan(choice, sequences)`: frozen dataclass; `choice` op id -> machine id, `sequences`
  machine id -> tuple of op ids (the moves API's `apply(move, choice, sequences)` shape).
- `tabu_search(index, spec, start, params, deadline, *, record_trace=False) -> TabuResult`
  with `TabuResult(best: Plan, best_values: Mapping[ObjectiveKind, float],
  trace: tuple[Visit, ...])` and `Visit(plan, values, aspiration: bool)`: one `Visit` per
  move made (the start is not a visit), `aspiration` true when the move's attribute was on
  the tabu list and the move was taken because it beats the best (p.381). `trace` is empty
  unless `record_trace` (the solver never records, so a 600x8 run keeps no plan history).
- `simulated_annealing(index, spec, start, params, deadline, rng, *, record_trace=False)
  -> AnnealResult` with `AnnealResult(best, best_values, trace: tuple[AnnealStep, ...])` and
  `AnnealStep(plan, values, delta, beta, accepted)`: one step per iteration; `plan`/`values`
  are the candidate's, `delta` = `Scorer(spec, start_values).delta(candidate, current)`,
  `beta` the temperature used for that step. `rng` is `random.Random`-like: the move is
  drawn with `randrange`/`choice`; `rng.random()` is the acceptance draw and is called once
  per worse candidate only (accept when `rng.random() < exp(-delta / beta)`).
- `index` is a `kernel.index.ProblemIndex`, `spec` the compiled objective, `params` the raw
  `method_params` mapping, `deadline` a `time.monotonic()` instant checked before each
  iteration (a past deadline returns the start). Scoring uses no previous plan unless the
  keyword-only `previous_plan` / `churn_tolerance` are given.

Params (`profile.method_params[name]`; an unknown or invalid one gives UNKNOWN
`invalid_method_param`, DEV-15 convention):

- `tabu`: `tabu_length` int 5..9 (default 7), `iteration_limit` int >= 1 (moves made),
  `allow_any_tabu_length` bool (default false; test-only: lets `tabu_length` be any int
  >= 1 so Pinedo's length-2 example runs).
- `sa`: `sa_iterations` int >= 1 (default 20,000), `sa_cooling` float in (0, 1) (default
  0.995). `beta_1` = the `Scorer.delta` of a 5 percent worse primary, over ln 2;
  `beta_{k+1}` = `sa_cooling * beta_k`.

Start (both): `context.warm_start` decoded to sequences, else ATCS through the registry,
else the dispatch engine with `edd`. A warm start that does not decode is ignored.
"""

from __future__ import annotations

import importlib
import math
import random
import time
from collections.abc import Callable, Mapping
from types import ModuleType
from typing import Any

import pytest
from kernel_cases import load_request, machine, only_choice, op, random_job_shop, request_from

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, fast_values
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    Schedule,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

WT = ObjectiveKind.WEIGHTED_TARDINESS
WT_OBJECTIVE = {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
FAR = 60.0  # seconds to a deadline no test reaches
P1434 = "pinedo_14_3_4"
WARM_1434 = ("2-1", "1-1", "4-1", "3-1")
OPTIMUM_1434 = ("1-1", "4-1", "2-1", "3-1")
ATCS_1434 = ("4-1", "1-1", "2-1", "3-1")  # ATCS on 14.3.4 (test_atcs SEQ_1434), 440
EDD_1434 = ("3-1", "2-1", "1-1", "4-1")  # dues 1, 2, 4, 12: 970
# Pinedo p.383-384 with tabu length 2 from 2,1,4,3 (500), recomputed by brute force.
PINEDO_TRACE = (
    (("2-1", "4-1", "1-1", "3-1"), 436.0),
    (("4-1", "2-1", "1-1", "3-1"), 460.0),
    (("4-1", "1-1", "2-1", "3-1"), 440.0),
    (("1-1", "4-1", "2-1", "3-1"), 408.0),
)
PINEDO_PARAMS = {"tabu_length": 2, "allow_any_tabu_length": True}


# --- helpers ---------------------------------------------------------------------------------


def _leaf() -> ModuleType:
    """The leaf, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.algorithms.local_search")


def _with_params(request: SolveRequest, name: str, params: Mapping[str, Any]) -> SolveRequest:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {name: dict(params)}
    return SolveRequest.model_validate(data)


def _index(request: SolveRequest) -> tuple[ProblemIndex, ObjectiveSpec]:
    compiled = apply_overrides(request)
    return ProblemIndex.build(compiled.problem), compiled.profile.objective


def _decoded(
    request: SolveRequest, choice: Mapping[str, str], sequences: Mapping[str, Any]
) -> Decoded:
    index, _ = _index(request)
    decoded = decode(index, choice, sequences)
    assert isinstance(decoded, Decoded), decoded
    return decoded


def _warm(request: SolveRequest, order: tuple[str, ...]) -> Schedule:
    """The 14.3.4-style single-machine schedule that runs `order` on M1."""
    return _decoded(request, only_choice(request), {"M1": list(order)}).schedule("warm")


def _plan(request: SolveRequest, order: tuple[str, ...]) -> Any:
    return _leaf().Plan(choice=only_choice(request), sequences={"M1": order})


def _start_values(request: SolveRequest, order: tuple[str, ...]) -> Mapping[ObjectiveKind, float]:
    index, _ = _index(request)
    return fast_values(index, _decoded(request, only_choice(request), {"M1": order}), None, 0.0)


def _run(
    name: str,
    request: SolveRequest,
    *,
    warm_start: Schedule | None = None,
    seed: int = 0,
    budget: float = 30.0,
) -> SolveResult:
    result = solve(
        request, solver=get_solver(name), budget_seconds=budget, seed=seed, warm_start=warm_start
    )
    assert result.schedule.status == "FEASIBLE", (name, result.schedule.issues)
    assert result.verified, (name, result.issues)
    return result


def _order(result: SolveResult, machine_id: str = "M1") -> tuple[str, ...]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return tuple(a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.operation_id)))


def _codes(result: SolveResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _sequence(plan: Any, machine_id: str = "M1") -> tuple[str, ...]:
    return tuple(plan.sequences[machine_id])


def _single_machine(
    p: Mapping[str, float], due: Mapping[str, float], weight: Mapping[str, float]
) -> SolveRequest:
    """1 || sum w_j T_j with one op `<job>-1` per job on M1."""
    jobs = [
        {
            "id": j,
            "due": due[j],
            "weight_override": weight[j],
            "operations": [op(f"{j}-1", {"M1": p[j]}, center="WC-M1")],
        }
        for j in p
    ]
    problem = {
        "id": "single-machine-wt",
        "jobs": jobs,
        "machines": [machine("M1", center="WC-M1")],
        "work_centers": [{"id": "WC-M1"}],
    }
    return request_from(problem, profile={"objective": WT_OBJECTIVE})


def _rng(seed: int) -> random.Random:
    """The seeded generator SA takes (search randomness, not security)."""
    return random.Random(seed)  # noqa: S311 - reproducible search draws, not a secret


class FixedDraw:
    """A `random.Random` stand-in whose `random()` is a fixed draw; the rest is seeded."""

    def __init__(self, seed: int, draw: float) -> None:
        self._inner = _rng(seed)
        self.draw = draw
        self.draws = 0

    def random(self) -> float:
        self.draws += 1
        return self.draw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _anneal(
    request: SolveRequest, order: tuple[str, ...], params: Mapping[str, Any], rng: Any
) -> Any:
    index, spec = _index(request)
    return _leaf().simulated_annealing(
        index, spec, _plan(request, order), params, time.monotonic() + FAR, rng, record_trace=True
    )


# --- registration ----------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "deterministic"), [("tabu", True), ("sa", False)])
def test_tabu_and_sa_are_registered_improvement_methods_with_every_feature(
    name: str, deterministic: bool
) -> None:
    info = {i.name: i for i in available()}
    assert name in info, sorted(info)
    entry = info[name]
    assert entry.family == "improvement"
    assert entry.deterministic is deterministic
    assert entry.capabilities == frozenset(Feature)
    assert entry.module == "twinflow.scheduling.algorithms.local_search"
    solver = get_solver(name)
    assert solver.name == name
    assert solver.capabilities == frozenset(Feature)


# --- tabu: Pinedo Example 14.3.4 (REQ-108) ---------------------------------------------------


def test_tabu_search_visits_pinedos_trace_from_2_1_4_3_with_length_2() -> None:
    request = load_request(P1434)
    index, spec = _index(request)
    params = PINEDO_PARAMS | {"iteration_limit": 4}

    result = _leaf().tabu_search(
        index, spec, _plan(request, WARM_1434), params, time.monotonic() + FAR, record_trace=True
    )

    visited = [(_sequence(v.plan), v.values[WT]) for v in result.trace]
    assert visited == [(seq, pytest.approx(value)) for seq, value in PINEDO_TRACE]
    assert [v.aspiration for v in result.trace] == [False] * 4
    assert _sequence(result.best) == OPTIMUM_1434
    assert result.best_values[WT] == pytest.approx(408.0)


def test_tabu_search_keeps_the_best_408_when_it_walks_on_past_it() -> None:
    # Moves 5 and 6 go to 1,2,4,3 (480) and 2,1,4,3 (500): the best stays 408.
    request = load_request(P1434)
    index, spec = _index(request)
    params = PINEDO_PARAMS | {"iteration_limit": 6}

    result = _leaf().tabu_search(
        index, spec, _plan(request, WARM_1434), params, time.monotonic() + FAR, record_trace=True
    )

    assert [v.values[WT] for v in result.trace][4:] == pytest.approx([480.0, 500.0])
    assert _sequence(result.best) == OPTIMUM_1434
    assert result.best_values[WT] == pytest.approx(408.0)


def test_tabu_search_records_no_trace_by_default() -> None:
    request = load_request(P1434)
    index, spec = _index(request)
    params = PINEDO_PARAMS | {"iteration_limit": 4}
    plan = _plan(request, WARM_1434)

    result = _leaf().tabu_search(index, spec, plan, params, time.monotonic() + FAR)

    assert result.trace == ()
    assert result.best_values[WT] == pytest.approx(408.0)


def test_tabu_search_with_a_past_deadline_returns_the_start() -> None:
    request = load_request(P1434)
    index, spec = _index(request)

    result = _leaf().tabu_search(
        index, spec, _plan(request, WARM_1434), PINEDO_PARAMS, time.monotonic() - 1.0
    )

    assert _sequence(result.best) == WARM_1434
    assert result.best_values[WT] == pytest.approx(500.0)


def test_tabu_solver_from_the_warm_start_with_length_2_returns_1_4_2_3_at_408() -> None:
    request = _with_params(load_request(P1434), "tabu", PINEDO_PARAMS | {"iteration_limit": 4})
    result = _run("tabu", request, warm_start=_warm(request, WARM_1434))
    assert _order(result) == OPTIMUM_1434
    assert result.objective_values[WT] == pytest.approx(408.0)


def test_tabu_solver_with_default_params_reaches_408_from_the_warm_start() -> None:
    request = load_request(P1434)
    result = _run("tabu", request, warm_start=_warm(request, WARM_1434))
    assert result.objective_values[WT] == pytest.approx(408.0)
    assert _order(result) == OPTIMUM_1434


# --- tabu: aspiration (p.381) -----------------------------------------------------------------

# A 6-job 1 || sum w_j T_j instance found by brute force: with the default-range length 5 from
# 2,6,5,3,4,1 (199), move 6 swaps 1 and 4 again (tabu since move 3) to reach 119 < best 125;
# without aspiration the search would take 2,6,1,4,3,5 (121) instead.
ASP_P = {"1": 5.0, "2": 4.0, "3": 6.0, "4": 3.0, "5": 4.0, "6": 9.0}
ASP_DUE = {"1": 20.0, "2": 3.0, "3": 25.0, "4": 13.0, "5": 6.0, "6": 8.0}
ASP_WEIGHT = {"1": 9.0, "2": 9.0, "3": 4.0, "4": 3.0, "5": 2.0, "6": 6.0}
ASP_START = ("2-1", "6-1", "5-1", "3-1", "4-1", "1-1")
ASP_TRACE = (
    ("2,6,5,4,3,1", 185.0),
    ("2,6,5,4,1,3", 151.0),
    ("2,6,5,1,4,3", 139.0),
    ("2,6,1,5,4,3", 131.0),
    ("2,6,1,4,5,3", 125.0),
    ("2,6,4,1,5,3", 119.0),
)


def _jobs(order: str) -> tuple[str, ...]:
    return tuple(f"{j}-1" for j in order.split(","))


def test_tabu_aspiration_takes_a_tabu_move_that_beats_the_best() -> None:
    request = _single_machine(ASP_P, ASP_DUE, ASP_WEIGHT)
    index, spec = _index(request)
    params = {"tabu_length": 5, "iteration_limit": 6}

    result = _leaf().tabu_search(
        index, spec, _plan(request, ASP_START), params, time.monotonic() + FAR, record_trace=True
    )

    visited = [(_sequence(v.plan), v.values[WT]) for v in result.trace]
    assert visited == [(_jobs(seq), pytest.approx(value)) for seq, value in ASP_TRACE]
    assert [v.aspiration for v in result.trace] == [False] * 5 + [True]
    assert result.best_values[WT] == pytest.approx(119.0)


def test_tabu_refuses_a_tabu_move_that_does_not_beat_the_best() -> None:
    # Pinedo step 2: from 2,4,1,3 the reverse swap to 2,1,4,3 (500) is tabu and not a new
    # best, so 4,2,1,3 (460) is taken although it is worse than the current 436.
    request = load_request(P1434)
    index, spec = _index(request)
    params = PINEDO_PARAMS | {"iteration_limit": 2}

    result = _leaf().tabu_search(
        index, spec, _plan(request, WARM_1434), params, time.monotonic() + FAR, record_trace=True
    )

    assert _sequence(result.trace[1].plan) == ("4-1", "2-1", "1-1", "3-1")
    assert result.trace[1].aspiration is False


# --- tabu: params -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"tabu_length": 4},
        {"tabu_length": 10},
        {"tabu_length": 2},
        {"tabu_length": 7.5},
        {"tabu_length": "7"},
        {"tabu_length": True},
        {"tabu_length": 0, "allow_any_tabu_length": True},
        {"allow_any_tabu_length": "yes"},
        {"iteration_limit": 0},
        {"iteration_limit": -3},
        {"iteration_limit": 2.5},
        {"sa_iterations": 100},
        {"bogus": 1},
    ],
    ids=repr,
)
def test_tabu_refuses_an_invalid_or_unknown_param(params: dict[str, Any]) -> None:
    request = _with_params(load_request(P1434), "tabu", params)
    result = solve(request, solver=get_solver("tabu"), budget_seconds=10.0)
    assert result.schedule.status == "UNKNOWN"
    assert "invalid_method_param" in _codes(result), result.issues
    assert not result.verified


@pytest.mark.parametrize("length", [5, 9])
def test_tabu_accepts_the_default_range_bounds(length: int) -> None:
    request = _with_params(load_request(P1434), "tabu", {"tabu_length": length})
    result = _run("tabu", request, warm_start=_warm(request, WARM_1434))
    assert result.objective_values[WT] == pytest.approx(408.0)


def test_tabu_test_param_allows_a_length_outside_5_to_9() -> None:
    request = _with_params(load_request(P1434), "tabu", PINEDO_PARAMS)
    result = _run("tabu", request, warm_start=_warm(request, WARM_1434))
    assert "invalid_method_param" not in _codes(result)


# --- tabu: start, monotonicity and determinism ------------------------------------------------


def test_tabu_without_a_warm_start_starts_from_the_atcs_schedule() -> None:
    # One move from ATCS's 4,1,2,3 (440) reaches 1,4,2,3 (408); from EDD's 3,2,1,4 it
    # would reach 2,3,1,4 (824), and from the warm start 2,4,1,3 (436).
    request = _with_params(load_request(P1434), "tabu", {"iteration_limit": 1})
    result = _run("tabu", request)
    assert _order(result) == OPTIMUM_1434
    assert result.objective_values[WT] == pytest.approx(408.0)


def test_tabu_one_move_from_the_warm_start_lands_on_its_first_pinedo_neighbor() -> None:
    request = _with_params(load_request(P1434), "tabu", {"iteration_limit": 1})
    result = _run("tabu", request, warm_start=_warm(request, WARM_1434))
    assert _order(result) == PINEDO_TRACE[0][0]
    assert result.objective_values[WT] == pytest.approx(436.0)


def _without_atcs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `atc` and `atcs` unregistered for this test (every registry lookup sees it)."""
    entries: Callable[[], Mapping[str, Any]] = registry._entries
    filtered = {k: v for k, v in entries().items() if k not in {"atc", "atcs"}}
    monkeypatch.setattr(registry, "_entries", lambda: filtered)


@pytest.mark.parametrize("name", ["tabu", "sa"])
def test_without_atcs_the_start_is_the_edd_dispatch_schedule(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _without_atcs(monkeypatch)
    assert not registry.is_registered("atcs")
    limit = {"tabu": "iteration_limit", "sa": "sa_iterations"}[name]
    request = _with_params(load_request(P1434), name, {limit: 1})

    result = _run(name, request)

    if name == "tabu":  # one best move from 3,2,1,4 (970)
        assert _order(result) == ("2-1", "3-1", "1-1", "4-1")
        assert result.objective_values[WT] == pytest.approx(824.0)
    else:  # one random neighbor of 3,2,1,4: best of start and candidate, never ATCS's 440
        assert result.objective_values[WT] in {970.0, 824.0, 950.0, 906.0}


@pytest.mark.parametrize("name", ["tabu", "sa"])
def test_a_warm_start_that_does_not_decode_falls_back_to_the_atcs_start(name: str) -> None:
    limit = {"tabu": "iteration_limit", "sa": "sa_iterations"}[name]
    request = _with_params(load_request(P1434), name, {limit: 1})
    empty = Schedule(status="UNKNOWN", assignments=(), solver="nobody", elapsed_seconds=0.0)

    fallback = _run(name, request, warm_start=empty)
    plain = _run(name, request)

    assert fallback.schedule.assignments == plain.schedule.assignments


@pytest.mark.parametrize("name", ["tabu", "sa"])
@pytest.mark.parametrize("seed", range(12))
def test_result_is_never_worse_than_the_warm_start(name: str, seed: int) -> None:
    shop, choice, sequences = random_job_shop(seed, setups=seed % 2 == 1)
    limit = {"tabu": "iteration_limit", "sa": "sa_iterations"}[name]
    request = _with_params(shop, name, {limit: 30})
    index, spec = _index(request)
    decoded = _decoded(request, choice, sequences)
    warm_values = fast_values(index, decoded, None, 0.0)

    result = _run(name, request, warm_start=decoded.schedule("warm"), seed=seed)

    scorer = Scorer(spec, warm_values)
    assert not scorer.better(warm_values, result.objective_values), (
        seed,
        dict(warm_values),
        result.objective_values,
    )


def test_tabu_is_deterministic_and_ignores_the_seed() -> None:
    shop, _, _ = random_job_shop(7, setups=True)
    request = _with_params(shop, "tabu", {"iteration_limit": 40})
    runs = [_run("tabu", request, seed=s) for s in (0, 0, 5)]
    assert runs[0].schedule.assignments == runs[1].schedule.assignments
    assert runs[0].schedule.assignments == runs[2].schedule.assignments


# --- simulated annealing (REQ-109) ------------------------------------------------------------


@pytest.mark.parametrize("seed", range(10))
def test_sa_reaches_408_on_14_3_4_for_every_seed_within_the_default_iteration_limit(
    seed: int,
) -> None:
    result = _run("sa", load_request(P1434), seed=seed)
    assert result.objective_values[WT] == pytest.approx(408.0), seed
    assert _order(result) == OPTIMUM_1434


def test_sa_same_seed_gives_the_same_result() -> None:
    shop, _, _ = random_job_shop(3, setups=True)
    request = _with_params(shop, "sa", {"sa_iterations": 300})
    first = _run("sa", request, seed=11)
    second = _run("sa", request, seed=11)
    assert first.schedule.assignments == second.schedule.assignments


def test_sa_pure_function_with_the_same_rng_seed_gives_the_same_trace() -> None:
    request = load_request(P1434)
    params = {"sa_iterations": 50}
    runs = [_anneal(request, WARM_1434, params, _rng(7)) for _ in range(2)]
    traces = [[(_sequence(s.plan), s.accepted) for s in run.trace] for run in runs]
    assert traces[0] == traces[1]
    assert len(traces[0]) == 50


def test_sa_first_beta_accepts_a_5_percent_worse_move_with_probability_one_half() -> None:
    request = load_request(P1434)
    start_values = _start_values(request, OPTIMUM_1434)
    _, spec = _index(request)
    worse = dict(start_values) | {WT: start_values[WT] * 1.05}
    delta_5 = Scorer(spec, start_values).delta(worse, start_values)

    run = _anneal(request, OPTIMUM_1434, {"sa_iterations": 5}, _rng(0))

    assert delta_5 > 0
    assert math.exp(-delta_5 / run.trace[0].beta) == pytest.approx(0.5)


@pytest.mark.parametrize("cooling", [0.995, 0.9])
def test_sa_beta_cools_geometrically_each_iteration(cooling: float) -> None:
    request = load_request(P1434)
    params = {"sa_iterations": 40, "sa_cooling": cooling}
    run = _anneal(request, WARM_1434, params, _rng(1))
    betas = [s.beta for s in run.trace]
    assert len(betas) == 40
    for k in range(39):
        assert betas[k + 1] == pytest.approx(cooling * betas[k], rel=1e-12), k


@pytest.mark.parametrize("draw", [0.0, 0.3, 0.6, 1.0 - 1e-12])
def test_sa_accepts_a_worse_move_iff_the_draw_is_below_exp_minus_delta_over_beta(
    draw: float,
) -> None:
    request = load_request(P1434)
    start_values = _start_values(request, OPTIMUM_1434)
    _, spec = _index(request)
    scorer = Scorer(spec, start_values)
    rng = FixedDraw(seed=4, draw=draw)

    run = _anneal(request, OPTIMUM_1434, {"sa_iterations": 60}, rng)

    current = start_values
    best = start_values[WT]
    current_seq = OPTIMUM_1434
    for k, step in enumerate(run.trace):
        assert _is_adjacent_swap(current_seq, _sequence(step.plan)), (k, current_seq, step.plan)
        assert step.delta == pytest.approx(scorer.delta(step.values, current), abs=1e-12), k
        if step.delta < 0:
            assert step.accepted, k
        elif step.delta > 0:
            assert step.accepted == (draw < math.exp(-step.delta / step.beta)), (k, step)
        if step.accepted:
            current, current_seq = step.values, _sequence(step.plan)
            best = min(best, step.values[WT])
    worse = sum(1 for s in run.trace if s.delta > 0)
    worse_or_equal = sum(1 for s in run.trace if s.delta >= 0)
    assert worse > 0, "the start is the optimum, so some candidate must be worse"
    assert worse <= rng.draws <= worse_or_equal
    assert run.best_values[WT] == pytest.approx(best)


def test_sa_with_draw_0_accepts_every_worse_move_and_with_draw_near_1_rejects_them() -> None:
    request = load_request(P1434)
    hot = _anneal(request, OPTIMUM_1434, {"sa_iterations": 30}, FixedDraw(2, 0.0))
    cold = _anneal(request, OPTIMUM_1434, {"sa_iterations": 30}, FixedDraw(2, 1.0 - 1e-12))
    assert all(s.accepted for s in hot.trace)
    assert not any(s.accepted for s in cold.trace if s.delta > 0)
    # from the optimum every neighbor is worse, so the cold run never leaves it
    assert all(_is_adjacent_swap(OPTIMUM_1434, _sequence(s.plan)) for s in cold.trace)
    assert hot.best_values[WT] == pytest.approx(408.0)
    assert cold.best_values[WT] == pytest.approx(408.0)


def _is_adjacent_swap(before: tuple[str, ...], after: tuple[str, ...]) -> bool:
    diff = [k for k, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    return (
        len(diff) == 2
        and diff[1] == diff[0] + 1
        and (before[diff[0]], before[diff[1]]) == (after[diff[1]], after[diff[0]])
    )


@pytest.mark.parametrize(
    "params",
    [
        {"sa_iterations": 0},
        {"sa_iterations": 1.5},
        {"sa_iterations": True},
        {"sa_cooling": 0.0},
        {"sa_cooling": 1.0},
        {"sa_cooling": 1.2},
        {"sa_cooling": "fast"},
        {"tabu_length": 7},
        {"bogus": 1},
    ],
    ids=repr,
)
def test_sa_refuses_an_invalid_or_unknown_param(params: dict[str, Any]) -> None:
    request = _with_params(load_request(P1434), "sa", params)
    result = solve(request, solver=get_solver("sa"), budget_seconds=10.0)
    assert result.schedule.status == "UNKNOWN"
    assert "invalid_method_param" in _codes(result), result.issues


def test_sa_from_the_optimal_warm_start_never_returns_worse() -> None:
    request = _with_params(load_request(P1434), "sa", {"sa_iterations": 200})
    result = _run("sa", request, warm_start=_warm(request, OPTIMUM_1434), seed=3)
    assert result.objective_values[WT] == pytest.approx(408.0)


@pytest.mark.parametrize("name", ["tabu", "sa"])
def test_a_tiny_budget_still_returns_a_verified_schedule(name: str) -> None:
    shop, _, _ = random_job_shop(5, setups=True)
    result = _run(name, shop, budget=0.01)
    assert len(result.schedule.assignments) > 0


# --- the ATCS start is what the docstring says -----------------------------------------------


def test_the_atcs_and_edd_reference_sequences_on_14_3_4() -> None:
    request = load_request(P1434)
    assert _order(_run("atcs", request)) == ATCS_1434
    assert _order(_run("edd", request)) == EDD_1434
