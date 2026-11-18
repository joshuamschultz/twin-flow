"""Fast evaluator and scorer (T-121, COMP-006, REQ-111, REQ-089).

`fast_values(index, decoded, previous_plan, churn_tolerance)` returns all thirteen
objective values of a decoded schedule; they must equal `objectives.evaluate` within 1e-9.
The generated problems get machine hour costs here (the generator leaves them 0), so the
`cost` term is not trivially 0, and a synthetic previous plan (moved starts, changed
machines, an op the problem does not have) so `churn` is not trivially 0.

`Scorer(spec, reference)` (`reference`: objective values or None) mirrors
`objectives.rank`:

- `key(values)`: weighted sum -> (fsum of weight * signed(value) / scale in term order,)
  with scale = term.scale, else |reference value|, else 1 (rank's exact arithmetic, so
  exact ties tie the same way); lexicographic -> signed values by level; epsilon ->
  (0, signed primary) when every bound holds, else (1, total violation).
- `better(a, b)`: a strictly before b. Lexicographic compares level by level and treats a
  difference within the level's tolerance as equal (relative: tolerance * |better of the
  two signed values|), which is exactly `rank`'s banding on two candidates.
- `delta(a, b)`: negative when a is better, positive when b is, 0 on a tie.

Agreement with `rank`: the full order by (key, id) equals rank for weighted sum (with and
without scales), epsilon-constraint and zero-tolerance lexicographic; for every mode,
`better` on every pair matches rank's relative order of that pair.
"""

from __future__ import annotations

import functools
import itertools
from collections.abc import Mapping
from typing import Any

import numpy as np
import pytest
from generators import random_request
from kernel_cases import compile_request, machine_choice, machine_sequences

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import Scorer, fast_values
from twinflow.scheduling.model import (
    CompiledProblem,
    EpsilonBound,
    EpsilonConstraint,
    FrozenZone,
    Lexicographic,
    LexicographicLevel,
    ObjectiveKind,
    ObjectiveSpec,
    PreviousAssignment,
    PreviousPlan,
    Schedule,
    SolveRequest,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.objectives import evaluate, rank
from twinflow.scheduling.solver import solve

SEEDS = range(200)
VALUE_TOL = 1e-9
MIN_COMPARED = 170
SETS_PER_MODE = 50
KINDS = tuple(ObjectiveKind)
Values = dict[ObjectiveKind, float]


# --- fast_values ---------------------------------------------------------------------------------


def _with_hour_costs(request: SolveRequest) -> SolveRequest:
    data = request.model_dump()
    for k, m in enumerate(data["problem"]["machines"]):
        m["hour_cost"] = 10.0 + k
        m["setup_hour_cost"] = 2.5 * k
    return SolveRequest.model_validate(data)


def _with_tolerance(compiled: CompiledProblem, tolerance: float) -> CompiledProblem:
    """`evaluate` reads churn tolerance from the profile's frozen zone; set it to `tolerance`."""
    zone = FrozenZone(until=compiled.problem.now, tolerance=tolerance)
    profile = compiled.profile.model_copy(update={"frozen_zone": zone})
    return compiled.model_copy(update={"profile": profile})


def _synthetic_plan(schedule: Schedule, seed: int) -> PreviousPlan:
    """Every third op moved 40 s, every fourth on a made-up machine, plus a ghost op."""
    moved = [
        PreviousAssignment(
            operation_id=a.operation_id,
            machine_id="elsewhere" if k % 4 == 0 else a.machine_id,
            start=a.start + (40.0 if k % 3 == 0 else 0.0),
        )
        for k, a in enumerate(schedule.assignments)
    ]
    ghost = PreviousAssignment(operation_id="ghost", machine_id="M0", start=1.0)
    return PreviousPlan(plan_id=f"prev-{seed}", problem_digest="d", assignments=(*moved, ghost))


@functools.cache
def _decoded(seed: int) -> tuple[CompiledProblem, ProblemIndex, Decoded] | None:
    request = _with_hour_costs(random_request(seed))
    source = solve(request)
    if not source.verified:
        return None
    compiled = compile_request(request)
    index = ProblemIndex.build(compiled.problem)
    schedule = source.schedule
    decoded = decode(index, machine_choice(schedule), machine_sequences(schedule))
    if not isinstance(decoded, Decoded):
        return None
    return compiled, index, decoded


def _assert_same_values(fast: Mapping[ObjectiveKind, float], slow: Values, seed: int) -> None:
    assert set(fast) == set(ObjectiveKind), f"seed {seed}: kinds {set(fast)}"
    for kind in ObjectiveKind:
        assert abs(fast[kind] - slow[kind]) <= VALUE_TOL, f"seed {seed} {kind}: {fast[kind]}"


def test_fast_values_equal_evaluate_without_previous_plan_on_200_problems() -> None:
    compared = 0
    for seed in SEEDS:
        case = _decoded(seed)
        if case is None:
            continue
        compiled, index, decoded = case
        slow = evaluate(_with_tolerance(compiled, 0.0), decoded.schedule("t"), None)
        _assert_same_values(fast_values(index, decoded, None, 0.0), slow, seed)
        compared += 1
    assert compared >= MIN_COMPARED


@pytest.mark.parametrize("tolerance", [0.0, 30.0, 1000.0])
def test_fast_values_equal_evaluate_with_previous_plan_on_200_problems(tolerance: float) -> None:
    compared = churned = 0
    for seed in SEEDS:
        case = _decoded(seed)
        if case is None:
            continue
        compiled, index, decoded = case
        schedule = decoded.schedule("t")
        plan = _synthetic_plan(schedule, seed)
        slow = evaluate(_with_tolerance(compiled, tolerance), schedule, plan)
        _assert_same_values(fast_values(index, decoded, plan, tolerance), slow, seed)
        compared += 1
        churned += slow[ObjectiveKind.CHURN] > 0
    assert compared >= MIN_COMPARED
    assert churned >= MIN_COMPARED // 2


def test_fast_values_cost_term_is_exercised() -> None:
    costed = [c for s in SEEDS if (c := _decoded(s)) is not None]
    assert any(
        fast_values(index, decoded, None, 0.0)[ObjectiveKind.COST] > 0
        for _, index, decoded in costed
    )


# --- Scorer --------------------------------------------------------------------------------------


def _values(rng: np.random.Generator) -> Values:
    """Small integer-ish values so exact ties and tolerance bands occur often."""
    values = {kind: float(rng.integers(0, 6)) for kind in KINDS}
    values[ObjectiveKind.MAX_LATENESS] = float(rng.integers(-3, 4))
    values[ObjectiveKind.ROBUSTNESS] = float(rng.integers(0, 5)) / 4
    return values


def _candidates(rng: np.random.Generator) -> dict[str, Values]:
    return {f"c{k:02d}": _values(rng) for k in range(int(rng.integers(2, 9)))}


def _kinds(rng: np.random.Generator, count: int) -> list[ObjectiveKind]:
    return [KINDS[int(i)] for i in rng.permutation(len(KINDS))[:count]]


def _weighted(rng: np.random.Generator, scaled: bool) -> WeightedSum:
    terms = [
        WeightedTerm(
            kind=kind,
            weight=float(rng.choice([0.5, 1.0, 2.0])),
            scale=float(rng.choice([0.5, 2.0, 10.0])) if scaled else None,
        )
        for kind in _kinds(rng, int(rng.integers(1, 4)))
    ]
    return WeightedSum(terms=tuple(terms))


def _lexicographic(rng: np.random.Generator, tolerance_kind: str, zero: bool) -> Lexicographic:
    choices = [0.1, 0.5] if tolerance_kind == "relative" else [0.0, 1.0, 2.5]
    levels = [
        LexicographicLevel(
            kind=kind,
            tolerance=0.0 if zero else float(rng.choice(choices)),
            tolerance_kind=tolerance_kind,  # type: ignore[arg-type]  # one of the two literals
        )
        for kind in _kinds(rng, int(rng.integers(1, 4)))
    ]
    return Lexicographic(levels=tuple(levels))


def _epsilon(rng: np.random.Generator) -> EpsilonConstraint:
    primary, *bounded = _kinds(rng, int(rng.integers(2, 4)))
    bounds = [EpsilonBound(kind=k, upper=float(rng.integers(1, 5))) for k in bounded]
    return EpsilonConstraint(primary=primary, bounds=tuple(bounds))


MODES: dict[str, Any] = {
    "weighted_sum": lambda rng: _weighted(rng, scaled=False),
    "weighted_sum_scaled": lambda rng: _weighted(rng, scaled=True),
    "lexicographic_zero": lambda rng: _lexicographic(rng, "absolute", zero=True),
    "lexicographic_absolute": lambda rng: _lexicographic(rng, "absolute", zero=False),
    "lexicographic_relative": lambda rng: _lexicographic(rng, "relative", zero=False),
    "epsilon_constraint": _epsilon,
}
TOTAL_ORDER_MODES = (
    "weighted_sum",
    "weighted_sum_scaled",
    "lexicographic_zero",
    "epsilon_constraint",
)


def _cases(mode: str) -> list[tuple[ObjectiveSpec, dict[str, Values], str]]:
    rng = np.random.default_rng((121, list(MODES).index(mode)))
    cases = []
    for _ in range(SETS_PER_MODE):
        spec = MODES[mode](rng)
        candidates = _candidates(rng)
        cases.append((spec, candidates, min(candidates)))
    return cases


def _pair_order(
    spec: ObjectiveSpec, candidates: Mapping[str, Values], reference: str, a: str, b: str
) -> list[str]:
    """rank's order of the pair {a, b}.

    Weighted sum scores each candidate alone given the reference, so the reference joins
    the set; any other mode ranks the two alone (a third candidate would change bands).
    """
    if isinstance(spec, WeightedSum):
        subset = {cid: candidates[cid] for cid in {a, b, reference}}
        order = rank(subset, spec, reference)
    else:
        order = rank({a: candidates[a], b: candidates[b]}, spec)
    return [cid for cid in order if cid in (a, b)]


@pytest.mark.parametrize("mode", TOTAL_ORDER_MODES)
def test_scorer_key_order_equals_rank(mode: str) -> None:
    for spec, candidates, reference in _cases(mode):
        scorer = Scorer(spec, candidates[reference])

        by_key = sorted(candidates, key=lambda cid: (scorer.key(candidates[cid]), cid))

        assert by_key == rank(candidates, spec, reference), spec


@pytest.mark.parametrize("mode", list(MODES))
def test_scorer_better_agrees_with_rank_on_every_pair(mode: str) -> None:
    decided = 0
    for spec, candidates, reference in _cases(mode):
        scorer = Scorer(spec, candidates[reference])
        for a, b in itertools.permutations(sorted(candidates), 2):
            va, vb = candidates[a], candidates[b]
            order = _pair_order(spec, candidates, reference, a, b)
            if scorer.better(va, vb):
                assert order == [a, b], (spec, a, b)
                decided += 1
            elif scorer.better(vb, va):
                assert order == [b, a], (spec, a, b)
            else:  # a tie: rank falls back to ids
                assert order == sorted([a, b]), (spec, a, b)
    assert decided > 0


def test_scorer_lexicographic_within_tolerance_falls_to_next_level() -> None:
    spec = Lexicographic(
        levels=(
            LexicographicLevel(kind=ObjectiveKind.MAKESPAN, tolerance=1.0),
            LexicographicLevel(kind=ObjectiveKind.TOTAL_SETUP),
        )
    )
    a = dict.fromkeys(KINDS, 0.0) | {ObjectiveKind.MAKESPAN: 10.0, ObjectiveKind.TOTAL_SETUP: 5.0}
    b = a | {ObjectiveKind.MAKESPAN: 10.9, ObjectiveKind.TOTAL_SETUP: 1.0}
    scorer = Scorer(spec, None)

    assert scorer.better(b, a)
    assert not scorer.better(a, b)
    assert rank({"a": a, "b": b}, spec) == ["b", "a"]


def test_scorer_weighted_sum_uses_reference_scale_and_one_for_zero() -> None:
    spec = WeightedSum(
        terms=(
            WeightedTerm(kind=ObjectiveKind.MAKESPAN, weight=1.0),
            WeightedTerm(kind=ObjectiveKind.TOTAL_SETUP, weight=1.0),
        )
    )
    reference = dict.fromkeys(KINDS, 0.0) | {ObjectiveKind.MAKESPAN: 100.0}
    scorer = Scorer(spec, reference)
    values = dict.fromkeys(KINDS, 0.0) | {
        ObjectiveKind.MAKESPAN: 50.0,
        ObjectiveKind.TOTAL_SETUP: 3.0,
    }

    assert scorer.key(values) == pytest.approx((50.0 / 100.0 + 3.0 / 1.0,))


def test_scorer_robustness_is_maximized() -> None:
    spec = Lexicographic(levels=(LexicographicLevel(kind=ObjectiveKind.ROBUSTNESS),))
    low = dict.fromkeys(KINDS, 0.0) | {ObjectiveKind.ROBUSTNESS: 0.2}
    high = low | {ObjectiveKind.ROBUSTNESS: 0.9}

    assert Scorer(spec, None).better(high, low)


@pytest.mark.parametrize("mode", list(MODES))
def test_scorer_delta_sign_agrees_with_better(mode: str) -> None:
    for spec, candidates, reference in _cases(mode):
        scorer = Scorer(spec, candidates[reference])
        for va, vb in itertools.product(candidates.values(), repeat=2):
            delta = scorer.delta(va, vb)
            if scorer.better(va, vb):
                assert delta < 0, spec
            elif scorer.better(vb, va):
                assert delta > 0, spec
            else:
                assert delta == 0, spec
        for values in candidates.values():
            assert not scorer.better(values, values)
            assert scorer.delta(values, values) == 0
