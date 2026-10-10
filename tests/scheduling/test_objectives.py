"""Tests for the objective evaluator and `rank` (T-091, T-092, COMP-005).

Every expected value is a hand calculation from the SDD "Objective formulas".
Requirements: REQ-068, REQ-069. Decision: D-068.
"""

from __future__ import annotations

import random
from collections.abc import Mapping

import pytest

from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    EpsilonBound,
    EpsilonConstraint,
    FrozenZone,
    Job,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    Operation,
    PreviousAssignment,
    PreviousPlan,
    Problem,
    ProcessingOption,
    Schedule,
    SchedulingProfile,
    WeightedSum,
    WeightedTerm,
    WorkCenter,
)
from twinflow.scheduling.objectives import evaluate, rank

K = ObjectiveKind


def _op(op_id: str, *options: tuple[str, float]) -> Operation:
    return Operation(
        id=op_id,
        work_center_id="wc",
        quantity=1.0,
        options=tuple(
            ProcessingOption(
                machine_id=machine_id, fixed_seconds=1.0, unit_seconds=0.0, preference_cost=cost
            )
            for machine_id, cost in options
        ),
    )


def _assign(op_id: str, machine_id: str, start: float, setup_end: float, end: float) -> Assignment:
    return Assignment(
        operation_id=op_id, machine_id=machine_id, start=start, setup_end=setup_end, end=end
    )


def _schedule(*assignments: Assignment) -> Schedule:
    return Schedule(status="FEASIBLE", assignments=assignments, solver="test", elapsed_seconds=0.0)


# --- hand-built 3-job, 2-machine schedule -----------------------------------------------------
#
# Profile class weights {1: 1, 2: 3, 4: 5}.
# J1: class 2 -> w 3, due 100, ops o11 (M1 pref 0 | M2 pref 5) then o12 (M2 pref 1).
# J2: weight_override 2, due 50, op o21 (M1 pref 2).
# J3: class 1 -> w 1, no due, op o31 (M2 pref 0).
# M1: hour_cost 36, setup_hour_cost 72. M2: hour_cost 18, setup_hour_cost 0.
#
# o11 M1 [0, 10, 40]   o21 M1 [40, 40, 70]   o31 M2 [0, 0, 30]   o12 M2 [40, 50, 90]
# C: J1 90, J2 70, J3 30.

PROFILE = SchedulingProfile(class_weights={1: 1.0, 2: 3.0, 4: 5.0})


def _problem(now: float = 0.0, extra_jobs: tuple[Job, ...] = ()) -> Problem:
    jobs = (
        Job(
            id="J1",
            weight_class=2,
            due=now + 100.0,
            operations=(_op("o11", ("M1", 0.0), ("M2", 5.0)), _op("o12", ("M2", 1.0))),
        ),
        Job(id="J2", weight_override=2.0, due=now + 50.0, operations=(_op("o21", ("M1", 2.0)),)),
        Job(id="J3", operations=(_op("o31", ("M2", 0.0)),)),
    )
    return Problem(
        id="hand",
        now=now,
        jobs=jobs + extra_jobs,
        machines=(
            Machine(id="M1", work_center_id="wc", hour_cost=36.0, setup_hour_cost=72.0),
            Machine(id="M2", work_center_id="wc", hour_cost=18.0, setup_hour_cost=0.0),
        ),
        work_centers=(WorkCenter(id="wc"),),
    )


def _hand_schedule(shift: float = 0.0) -> Schedule:
    return _schedule(
        _assign("o11", "M1", shift + 0.0, shift + 10.0, shift + 40.0),
        _assign("o21", "M1", shift + 40.0, shift + 40.0, shift + 70.0),
        _assign("o31", "M2", shift + 0.0, shift + 0.0, shift + 30.0),
        _assign("o12", "M2", shift + 40.0, shift + 50.0, shift + 90.0),
    )


HAND_VALUES: dict[ObjectiveKind, float] = {
    K.MAKESPAN: 90.0,
    K.WEIGHTED_COMPLETION: 3 * 90 + 2 * 70 + 1 * 30,  # 440
    K.MAX_LATENESS: 20.0,  # max(90 - 100, 70 - 50)
    K.TARDY_COUNT: 1.0,
    K.WEIGHTED_TARDY_COUNT: 2.0,
    K.TOTAL_TARDINESS: 20.0,
    K.WEIGHTED_TARDINESS: 40.0,
    K.EARLINESS_TARDINESS: 30.0,  # |90 - 100| + |70 - 50|
    K.TOTAL_SETUP: 20.0,  # 10 + 0 + 0 + 10
    K.COST: (30 * 36 + 10 * 72 + 30 * 36 + 30 * 18 + 40 * 18) / 3600,  # 4140 / 3600
    K.CHURN: 0.0,
    K.ROBUSTNESS: (3 * (100 - 90) + 2 * (50 - 70)) / (3 * 100 + 2 * 50),  # -10 / 400
    K.SOFT_PENALTY: 0.0 + 2.0 + 0.0 + 1.0,
}


def test_evaluate_hand_built_schedule_matches_every_hand_value() -> None:
    values = evaluate(CompiledProblem(problem=_problem(), profile=PROFILE), _hand_schedule())
    assert set(values) == set(ObjectiveKind)
    for kind, expected in HAND_VALUES.items():
        assert values[kind] == pytest.approx(expected), kind


def test_evaluate_measures_times_from_now() -> None:
    shifted = CompiledProblem(problem=_problem(now=1000.0), profile=PROFILE)
    values = evaluate(shifted, _hand_schedule(shift=1000.0))
    assert values[K.MAKESPAN] == pytest.approx(90.0)
    assert values[K.ROBUSTNESS] == pytest.approx(HAND_VALUES[K.ROBUSTNESS])
    assert values[K.WEIGHTED_TARDINESS] == pytest.approx(40.0)


def test_evaluate_excludes_held_jobs() -> None:
    compiled = CompiledProblem(problem=_problem(), profile=PROFILE, held_job_ids=("J2",))
    schedule = _schedule(*(a for a in _hand_schedule().assignments if a.operation_id != "o21"))
    values = evaluate(compiled, schedule)
    assert values[K.TARDY_COUNT] == 0.0
    assert values[K.WEIGHTED_COMPLETION] == pytest.approx(3 * 90 + 1 * 30)
    assert values[K.SOFT_PENALTY] == pytest.approx(1.0)


def test_evaluate_ignores_assignments_of_held_jobs() -> None:
    compiled = CompiledProblem(problem=_problem(), profile=PROFILE, held_job_ids=("J2",))
    values = evaluate(compiled, _hand_schedule())
    assert values[K.WEIGHTED_TARDINESS] == 0.0
    assert values[K.TOTAL_SETUP] == pytest.approx(20.0)
    assert values[K.COST] == pytest.approx((4140 - 30 * 36) / 3600)


def test_evaluate_excludes_jobs_without_assignments() -> None:
    late = Job(id="J9", due=1.0, operations=(_op("o91", ("M1", 0.0)),))
    compiled = CompiledProblem(problem=_problem(extra_jobs=(late,)), profile=PROFILE)
    values = evaluate(compiled, _hand_schedule())
    for kind, expected in HAND_VALUES.items():
        assert values[kind] == pytest.approx(expected), kind


def test_evaluate_jobs_without_due_leave_due_terms_zero() -> None:
    job = Job(id="J", operations=(_op("o", ("M1", 0.0)),))
    problem = _problem().model_copy(update={"jobs": (job,)})
    values = evaluate(
        CompiledProblem(problem=problem, profile=PROFILE), _schedule(_assign("o", "M1", 0, 0, 50))
    )
    assert values[K.MAKESPAN] == 50.0
    for kind in (
        K.MAX_LATENESS,
        K.TARDY_COUNT,
        K.WEIGHTED_TARDY_COUNT,
        K.TOTAL_TARDINESS,
        K.WEIGHTED_TARDINESS,
        K.EARLINESS_TARDINESS,
        K.ROBUSTNESS,
    ):
        assert values[kind] == 0.0, kind


def test_evaluate_empty_schedule_is_all_zero() -> None:
    values = evaluate(CompiledProblem(problem=_problem(), profile=PROFILE), _schedule())
    assert values == dict.fromkeys(ObjectiveKind, 0.0)


def test_robustness_is_zero_when_denominator_not_positive() -> None:
    # One due job with due at `now`: denominator w * (d - now) = 0.
    job = Job(id="J", due=0.0, operations=(_op("o", ("M1", 0.0)),))
    problem = _problem().model_copy(update={"jobs": (job,)})
    values = evaluate(
        CompiledProblem(problem=problem, profile=PROFILE), _schedule(_assign("o", "M1", 0, 0, 5))
    )
    assert values[K.ROBUSTNESS] == 0.0


def test_robustness_matches_pinedo_example_18_1_1_shape() -> None:
    # R(S) = sum w (d - C) / sum w d with now = 0 (Pinedo p.483).
    jobs = (
        Job(id="A", weight_override=1.0, due=10.0, operations=(_op("a", ("M1", 0.0)),)),
        Job(id="B", weight_override=4.0, due=20.0, operations=(_op("b", ("M1", 0.0)),)),
    )
    problem = _problem().model_copy(update={"jobs": jobs})
    schedule = _schedule(_assign("a", "M1", 0, 0, 8), _assign("b", "M1", 8, 8, 18))
    values = evaluate(CompiledProblem(problem=problem, profile=PROFILE), schedule)
    assert values[K.ROBUSTNESS] == pytest.approx((1 * 2 + 4 * 2) / (1 * 10 + 4 * 20))


# --- churn ------------------------------------------------------------------------------------


def _previous_plan() -> PreviousPlan:
    return PreviousPlan(
        plan_id="p0",
        problem_digest="d0",
        assignments=(
            PreviousAssignment(operation_id="o11", machine_id="M1", start=0.0),  # same
            PreviousAssignment(operation_id="o21", machine_id="M2", start=40.0),  # machine changed
            PreviousAssignment(operation_id="o12", machine_id="M2", start=43.0),  # moved 3
            PreviousAssignment(operation_id="o31", machine_id="M2", start=10.0),  # moved 10
            PreviousAssignment(operation_id="gone", machine_id="M1", start=0.0),  # not scheduled
        ),
    )


def test_churn_counts_machine_changes_and_moves_beyond_frozen_tolerance() -> None:
    profile = PROFILE.model_copy(update={"frozen_zone": FrozenZone(until=0.0, tolerance=5.0)})
    compiled = CompiledProblem(problem=_problem(), profile=profile)
    values = evaluate(compiled, _hand_schedule(), _previous_plan())
    assert values[K.CHURN] == 2.0


def test_churn_tolerance_is_zero_without_frozen_zone() -> None:
    compiled = CompiledProblem(problem=_problem(), profile=PROFILE)
    values = evaluate(compiled, _hand_schedule(), _previous_plan())
    assert values[K.CHURN] == 3.0


# --- Pinedo example 14.2.1, sequence 2, 4, 3, 1 (p.377) ----------------------------------------


def test_pinedo_14_2_1_weighted_tardiness_and_setup() -> None:
    processing = {"1": 13.0, "2": 9.0, "3": 13.0, "4": 10.0}
    due = {"1": 12.0, "2": 37.0, "3": 21.0, "4": 22.0}
    weight = {"1": 2.0, "2": 4.0, "3": 2.0, "4": 5.0}
    jobs = tuple(
        Job(id=j, weight_override=weight[j], due=due[j], operations=(_op(f"op{j}", ("M", 0.0)),))
        for j in processing
    )
    problem = Problem(
        id="pinedo-14.2.1",
        jobs=jobs,
        machines=(Machine(id="M", work_center_id="wc"),),
        work_centers=(WorkCenter(id="wc"),),
    )
    # Setups from the p.377 matrix: start->2 = 1, 2->4 = 0, 4->3 = 1, 3->1 = 1.
    schedule = _schedule(
        _assign("op2", "M", 0.0, 1.0, 10.0),
        _assign("op4", "M", 10.0, 10.0, 20.0),
        _assign("op3", "M", 20.0, 21.0, 34.0),
        _assign("op1", "M", 34.0, 35.0, 48.0),
    )
    values = evaluate(CompiledProblem(problem=problem, profile=SchedulingProfile()), schedule)
    assert values[K.WEIGHTED_TARDINESS] == pytest.approx(98.0)
    assert values[K.TOTAL_SETUP] == pytest.approx(3.0)
    assert values[K.MAKESPAN] == pytest.approx(48.0)


# --- rank -------------------------------------------------------------------------------------


def _values(**by_kind: float) -> dict[ObjectiveKind, float]:
    return {ObjectiveKind(name): value for name, value in by_kind.items()}


def test_rank_weighted_sum_with_explicit_scales() -> None:
    spec = WeightedSum(
        terms=(
            WeightedTerm(kind=K.MAKESPAN, weight=1.0, scale=100.0),
            WeightedTerm(kind=K.COST, weight=2.0, scale=10.0),
        )
    )
    candidates = {
        "a": _values(makespan=100.0, cost=10.0),  # 1 + 2 = 3
        "b": _values(makespan=50.0, cost=10.0),  # 0.5 + 2 = 2.5
        "c": _values(makespan=200.0, cost=0.0),  # 2 + 0 = 2
    }
    assert rank(candidates, spec) == ["c", "b", "a"]


def test_rank_weighted_sum_normalizes_by_reference_candidate() -> None:
    spec = WeightedSum(
        terms=(WeightedTerm(kind=K.MAKESPAN, weight=1.0), WeightedTerm(kind=K.COST, weight=1.0))
    )
    candidates = {
        "ref": _values(makespan=100.0, cost=1000.0),  # 1 + 1 = 2
        "x": _values(makespan=50.0, cost=1500.0),  # 0.5 + 1.5 = 2.0 -> tie, id order
        "y": _values(makespan=150.0, cost=500.0),  # 1.5 + 0.5 = 2.0
        "z": _values(makespan=90.0, cost=1000.0),  # 0.9 + 1 = 1.9
    }
    assert rank(candidates, spec, reference="ref") == ["z", "ref", "x", "y"]
    # Unnormalized, cost dominates.
    assert rank(candidates, spec) == ["y", "z", "ref", "x"]


def test_rank_weighted_sum_zero_reference_uses_one() -> None:
    spec = WeightedSum(
        terms=(WeightedTerm(kind=K.CHURN, weight=1.0), WeightedTerm(kind=K.MAKESPAN, weight=1.0))
    )
    candidates = {
        "ref": _values(churn=0.0, makespan=10.0),  # 0 + 1 = 1
        "a": _values(churn=3.0, makespan=5.0),  # 3 + 0.5 = 3.5
        "b": _values(churn=0.5, makespan=9.0),  # 0.5 + 0.9 = 1.4
    }
    assert rank(candidates, spec, reference="ref") == ["ref", "b", "a"]


def test_rank_weighted_sum_negates_maximize_kinds() -> None:
    spec = WeightedSum(terms=(WeightedTerm(kind=K.ROBUSTNESS, weight=1.0),))
    candidates = {"lo": _values(robustness=0.1), "hi": _values(robustness=0.5)}
    assert rank(candidates, spec) == ["hi", "lo"]


def test_rank_lexicographic_absolute_tolerance_ties_then_next_level() -> None:
    spec = Lexicographic(
        levels=(
            LexicographicLevel(kind=K.WEIGHTED_TARDINESS, tolerance=5.0),
            LexicographicLevel(kind=K.MAKESPAN),
        )
    )
    candidates = {
        "a": _values(weighted_tardiness=100.0, makespan=90.0),
        "b": _values(weighted_tardiness=104.0, makespan=80.0),  # within 5 of best -> tied
        "c": _values(weighted_tardiness=106.0, makespan=10.0),  # outside -> next band
        "d": _values(weighted_tardiness=110.0, makespan=20.0),  # within 5 of c -> tied with c
    }
    assert rank(candidates, spec) == ["b", "a", "c", "d"]


def test_rank_lexicographic_relative_tolerance() -> None:
    spec = Lexicographic(
        levels=(
            LexicographicLevel(kind=K.COST, tolerance=0.1, tolerance_kind="relative"),
            LexicographicLevel(kind=K.MAKESPAN),
        )
    )
    candidates = {
        "a": _values(cost=200.0, makespan=50.0),
        "b": _values(cost=215.0, makespan=40.0),  # within 10% of 200 -> tied
        "c": _values(cost=225.0, makespan=1.0),  # outside 220
    }
    assert rank(candidates, spec) == ["b", "a", "c"]


def test_rank_lexicographic_zero_tolerance_is_strict() -> None:
    spec = Lexicographic(
        levels=(LexicographicLevel(kind=K.MAKESPAN), LexicographicLevel(kind=K.COST))
    )
    candidates = {
        "a": _values(makespan=10.0, cost=5.0),
        "b": _values(makespan=10.0, cost=1.0),
        "c": _values(makespan=9.0, cost=99.0),
    }
    assert rank(candidates, spec) == ["c", "b", "a"]


def test_rank_lexicographic_maximize_kind() -> None:
    spec = Lexicographic(levels=(LexicographicLevel(kind=K.ROBUSTNESS, tolerance=0.05),))
    candidates = {
        "a": _values(robustness=0.40),
        "b": _values(robustness=0.50),
        "c": _values(robustness=0.47),  # within 0.05 of 0.50 -> tied with b, id order
    }
    assert rank(candidates, spec) == ["b", "c", "a"]


def test_rank_epsilon_constraint_satisfiers_first_then_violators() -> None:
    spec = EpsilonConstraint(
        primary=K.WEIGHTED_TARDINESS,
        bounds=(
            EpsilonBound(kind=K.MAKESPAN, upper=100.0),
            EpsilonBound(kind=K.COST, upper=10.0),
        ),
    )
    candidates = {
        "ok_worse": _values(weighted_tardiness=50.0, makespan=100.0, cost=10.0),
        "ok_better": _values(weighted_tardiness=20.0, makespan=90.0, cost=5.0),
        "bad_small": _values(weighted_tardiness=0.0, makespan=101.0, cost=10.0),  # violation 1
        "bad_big": _values(weighted_tardiness=0.0, makespan=110.0, cost=15.0),  # violation 15
    }
    assert rank(candidates, spec) == ["ok_better", "ok_worse", "bad_small", "bad_big"]


def test_rank_ties_break_by_candidate_id() -> None:
    spec = WeightedSum(terms=(WeightedTerm(kind=K.MAKESPAN, weight=1.0),))
    candidates = {cid: _values(makespan=1.0) for cid in ("c", "a", "b")}
    assert rank(candidates, spec) == ["a", "b", "c"]


def _random_candidates(rng: random.Random) -> dict[str, dict[ObjectiveKind, float]]:
    return {
        f"cand-{index:02d}": {kind: float(rng.randint(0, 5)) for kind in ObjectiveKind}
        for index in range(12)
    }


@pytest.mark.parametrize(
    "spec",
    [
        WeightedSum(
            terms=(
                WeightedTerm(kind=K.MAKESPAN, weight=1.0),
                WeightedTerm(kind=K.ROBUSTNESS, weight=2.0),
            )
        ),
        Lexicographic(
            levels=(
                LexicographicLevel(kind=K.TARDY_COUNT, tolerance=1.0),
                LexicographicLevel(kind=K.COST, tolerance=0.2, tolerance_kind="relative"),
            )
        ),
        EpsilonConstraint(primary=K.MAKESPAN, bounds=(EpsilonBound(kind=K.COST, upper=2.0),)),
    ],
)
def test_rank_is_deterministic_across_shuffles(
    spec: WeightedSum | Lexicographic | EpsilonConstraint,
) -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test data, not cryptography
    candidates = _random_candidates(rng)
    expected = rank(candidates, spec, reference="cand-00")
    ids = list(candidates)
    for _ in range(50):
        rng.shuffle(ids)
        shuffled: Mapping[str, Mapping[ObjectiveKind, float]] = {
            cid: candidates[cid] for cid in ids
        }
        assert rank(shuffled, spec, reference="cand-00") == expected
