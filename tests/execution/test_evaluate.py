"""T-184 (RED): the evaluator: replications, budget, workers, statistics, pairing, ranking.

REQ-134, REQ-136, REQ-138, REQ-144; COMP-138 (SDD "Evaluation" pseudo-code and Data Model
"Outputs"); D-096, D-099, D-103, D-068.

API under test, `twinflow.execution.evaluate`:

- `CandidateInput{plan_id: str, compiled: CompiledProblem, schedule: Schedule}` (frozen).
- `evaluate_candidates(candidates, *, source, policy, targets, seed, replications,
  budget_seconds, workers, baseline_rule: str | None, clock=time.monotonic)
  -> EvaluationResult`. `source` is a `DistributionSource`, `policy` a `SimulationPolicy`
  (its `confidence_level` sets every interval, its robustness fields the robustness
  analysis), `targets` the profile's load targets. Replication r (0-based) of every
  candidate and of the baseline runs with `RepDraws(seed, r, source)`; the baseline runs
  `DispatchControl(baseline_rule)` on the FIRST candidate's compiled problem. Raises
  `ValueError` for no candidates, duplicate plan ids, `replications < 1`, `workers < 1` or
  a baseline rule outside `twinflow.policy.DISPATCH_NAMES`.
- Budget contract (D-099), exact so a fake clock pins it: the evaluator reads `clock` once
  when it starts and once before each (candidate or baseline, replication) submission,
  never in between; tasks go out replication-major (rep 0 of every candidate in input
  order, then the baseline, then rep 1, ...); a task is submitted only while
  `clock() - start < budget_seconds`. Only replications 0..k-1 that every candidate and
  the baseline finished are kept. Status `"truncated"` when k < the replications run for,
  else `"completed"`. k == 0 raises `BudgetExhausted` (`code == "budget_exhausted"`).
- `EvaluationResult{status, replications (k), deterministic, candidates (input order),
  baseline, paired, ranking, elapsed_seconds}`. `paired[plan_id][metric]` is the candidate
  minus the baseline over common replications for every name of `metrics.METRIC_NAMES`;
  `{}` without a baseline. `ranking` = `objectives.rank` over each candidate's mean
  objective values under the first candidate's `compiled.profile.objective` (no reference).
- `CandidateEvaluation{plan_id (None for the baseline), planned: Mapping[ObjectiveKind,
  float] (`objectives.evaluate` of the plan; empty for the baseline), per_rep:
  tuple[ReplicationMetrics, ...] (rep order), runs: tuple[ExecutionRun, ...] (rep order),
  metrics: Mapping[str, Stats] (every METRIC_NAMES name), robustness: RobustnessSummary |
  None (None for the baseline), bottleneck: Bottleneck | None (None for the baseline),
  operation_rows, job_rows (the `metrics.operation_rows` / `job_rows` evidence)}`.
  `Bottleneck{machine_id, planned_utilization}`: the planned top machine by busy share.
- `Stats{mean, std, min, p10, p50, p90, max, ci_low, ci_high, n}`; `stats(values,
  confidence) -> Stats`: numpy mean, std ddof=1 (0.0 when n == 1), linear percentiles,
  Student-t interval on the mean (None when n < 2).
- `PairedDelta{mean_difference, low, high, n}`; `paired_delta(candidate, reference,
  confidence) -> PairedDelta` over the common keys (replication indices) of two
  `{rep: value}` mappings, by the `DifferenceCI` method of `twinflow/plan/replication.py`
  (stderr 0 when the population std of the differences is 0); low/high None when n < 2.
  The execution package cannot import `twinflow.plan`, so expectations here are computed
  independently with numpy and scipy.

The module is imported lazily so each test fails at its first call into it.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pytest
import scipy.stats
from _engine_cases import assert_verified, at, job, op, plan, problem
from _plans import generate_core

from twinflow.execution import engine, metrics, robustness
from twinflow.execution.policy import Deterministic, FactorRule, LogNormal, SimulationPolicy
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource, ProfileSource
from twinflow.scheduling import SolveRequest, apply_overrides
from twinflow.scheduling.model import (
    CompiledProblem,
    Lexicographic,
    LexicographicLevel,
    Machine,
    ObjectiveKind,
    Schedule,
    SchedulingProfile,
)
from twinflow.scheduling.objectives import evaluate, rank

WT = ObjectiveKind.WEIGHTED_TARDINESS
TARGETS = (0.9, 0.8, 0.7)
SEED = 42
REL = 1e-12


class _LazyModule:
    """The module under test, imported at its first attribute access (after arrange)."""

    def __getattr__(self, name: str) -> Any:
        return getattr(importlib.import_module("twinflow.execution.evaluate"), name)


@pytest.fixture
def ev() -> Any:
    """`twinflow.execution.evaluate`, imported where a test first uses it."""
    return _LazyModule()


class FakeClock:
    """Returns 0, 1, 2, ... : each read advances one second."""

    def __init__(self) -> None:
        self.reads = 0

    def __call__(self) -> float:
        value = float(self.reads)
        self.reads += 1
        return value


# --- Pinedo Example 18.1.1 under a weighted-tardiness objective ------------------------------


def _pinedo() -> CompiledProblem:
    jobs = [
        job("J1", op("O1", "M", 10.0)).model_copy(update={"due": 10.0, "weight_override": 1.0}),
        job("J2", op("O2", "M", 10.0)).model_copy(update={"due": 22.0, "weight_override": 100.0}),
        job("J3", op("O3", "M", 10.0)).model_copy(update={"due": 34.0, "weight_override": 100.0}),
    ]
    profile = SchedulingProfile(objective=Lexicographic(levels=(LexicographicLevel(kind=WT),)))
    request = SolveRequest(
        schema_version="2.0",
        problem=problem(jobs, [Machine(id="M", work_center_id="A")]),
        profile=profile,
    )
    return apply_overrides(request)


def _sequence(order: str) -> Schedule:
    """The jobs' operations back to back on M from 0, in `order`."""
    return plan(
        *(at(f"O{j}", "M", 10.0 * i, 10.0 * i, 10.0 * (i + 1)) for i, j in enumerate(order))
    )


PINEDO = _pinedo()
PINEDO_SHOP = build_shop(PINEDO)
# Under the seed calibration the robust plan 2,3,1 has the lower mean weighted tardiness
# (Pinedo's point: 1,2,3 is optimal only without disruptions). Its id sorts LAST, and the
# ranking test passes it second, so a ranking by id or by input order cannot pass.
PLAN_231 = ("b-231", _sequence("231"))
PLAN_123 = ("a-123", _sequence("123"))

SEED_CALIBRATION = SimulationPolicy(
    distribution_source="profile",
    run_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),),
    setup_factors=(FactorRule(distribution=LogNormal(mean=1.1, cv=0.3)),),
)
STOCHASTIC = ProfileSource(SEED_CALIBRATION)


def _inputs(ev: Any, *plans: tuple[str, Schedule]) -> list[Any]:
    return [ev.CandidateInput(plan_id=pid, compiled=PINEDO, schedule=s) for pid, s in plans]


def _evaluate(ev: Any, candidates: Sequence[Any], **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "source": STOCHASTIC,
        "policy": SEED_CALIBRATION,
        "targets": TARGETS,
        "seed": SEED,
        "replications": 5,
        "budget_seconds": 600.0,
        "workers": 1,
        "baseline_rule": None,
    } | overrides
    return ev.evaluate_candidates(candidates, **arguments)


def _metric(per_rep: Sequence[Any], name: str) -> list[float]:
    return [float(getattr(rep, name)) for rep in per_rep]


# --- independent expectations (numpy / scipy) ------------------------------------------------


def _expected_stats(values: Sequence[float], confidence: float) -> dict[str, Any]:
    data = np.asarray(values, dtype=float)
    n = len(data)
    low: float | None = None
    high: float | None = None
    if n >= 2:
        sem = float(np.std(data, ddof=1)) / math.sqrt(n)
        t = float(scipy.stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1))
        low, high = float(data.mean()) - t * sem, float(data.mean()) + t * sem
    return {
        "mean": float(data.mean()),
        "std": float(np.std(data, ddof=1)) if n >= 2 else 0.0,
        "min": float(data.min()),
        "p10": float(np.percentile(data, 10, method="linear")),
        "p50": float(np.percentile(data, 50, method="linear")),
        "p90": float(np.percentile(data, 90, method="linear")),
        "max": float(data.max()),
        "ci_low": low,
        "ci_high": high,
        "n": n,
    }


def _expected_delta(
    candidate: Mapping[int, float], reference: Mapping[int, float], confidence: float
) -> dict[str, Any]:
    """The `DifferenceCI` method (plan/replication.py), recomputed with numpy and scipy."""
    common = sorted(set(candidate) & set(reference))
    diffs = np.asarray([candidate[r] - reference[r] for r in common], dtype=float)
    n = len(diffs)
    mean = float(diffs.mean())
    if n < 2:
        return {"mean_difference": mean, "low": None, "high": None, "n": n}
    t = float(scipy.stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1))
    spread = float(np.std(diffs, ddof=0))
    stderr = 0.0 if spread == 0.0 else float(np.std(diffs, ddof=1)) / math.sqrt(n)
    return {"mean_difference": mean, "low": mean - t * stderr, "high": mean + t * stderr, "n": n}


def _assert_matches(got: Any, expected: Mapping[str, Any]) -> None:
    for name, value in expected.items():
        actual = getattr(got, name)
        if value is None or isinstance(value, int):
            assert actual == value, name
        else:
            assert actual == pytest.approx(value, rel=REL, abs=1e-12), name


# --- fixture checks ---------------------------------------------------------------------------


def test_pinedo_fixture_plans_verify_and_the_source_is_stochastic() -> None:
    assert_verified(PINEDO, PLAN_231[1])
    assert_verified(PINEDO, PLAN_123[1])
    assert STOCHASTIC.is_stochastic(PINEDO_SHOP)
    assert not DeterministicSource().is_stochastic(PINEDO_SHOP)


# --- Stats -------------------------------------------------------------------------------------

KNOWN = (3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0)


def test_stats_known_sample_equals_numpy_and_scipy_t_interval_at_0_90(ev: Any) -> None:
    got = ev.stats(KNOWN, 0.90)

    _assert_matches(got, _expected_stats(KNOWN, 0.90))
    low, high = scipy.stats.t.interval(
        0.90, df=len(KNOWN) - 1, loc=np.mean(KNOWN), scale=scipy.stats.sem(KNOWN)
    )
    assert (got.ci_low, got.ci_high) == pytest.approx((low, high), rel=REL)


def test_stats_interval_follows_the_confidence_argument(ev: Any) -> None:
    narrow = ev.stats(KNOWN, 0.80)
    wide = ev.stats(KNOWN, 0.95)

    _assert_matches(narrow, _expected_stats(KNOWN, 0.80))
    _assert_matches(wide, _expected_stats(KNOWN, 0.95))
    assert wide.ci_high - wide.ci_low > narrow.ci_high - narrow.ci_low


def test_stats_single_value_has_no_interval_and_zero_std(ev: Any) -> None:
    got = ev.stats((7.5,), 0.90)

    _assert_matches(
        got,
        {
            "mean": 7.5,
            "std": 0.0,
            "min": 7.5,
            "p10": 7.5,
            "p50": 7.5,
            "p90": 7.5,
            "max": 7.5,
            "ci_low": None,
            "ci_high": None,
            "n": 1,
        },
    )


def test_stats_constant_sample_has_a_zero_width_interval_at_the_mean(ev: Any) -> None:
    got = ev.stats((4.0, 4.0, 4.0), 0.90)

    assert (got.std, got.ci_low, got.ci_high, got.n) == (0.0, 4.0, 4.0, 3)


# --- paired deltas ---------------------------------------------------------------------------


def test_paired_delta_uses_only_common_replications_by_the_difference_ci_method(ev: Any) -> None:
    candidate = {0: 12.0, 1: 15.5, 2: 9.0, 3: 20.25}
    reference = {1: 10.0, 2: 9.5, 3: 14.0, 4: 99.0}

    got = ev.paired_delta(candidate, reference, 0.90)

    _assert_matches(got, _expected_delta(candidate, reference, 0.90))
    assert got.n == 3


def test_paired_delta_of_a_plan_with_itself_is_zero_mean_and_zero_width(ev: Any) -> None:
    values = {0: 3.25, 1: 8.0, 2: 1.5, 3: 6.75}

    got = ev.paired_delta(values, dict(values), 0.90)

    assert (got.mean_difference, got.low, got.high, got.n) == (0.0, 0.0, 0.0, 4)


def test_paired_delta_constant_shift_has_zero_width_at_the_shift(ev: Any) -> None:
    reference = {0: 1.0, 1: 5.0, 2: 2.0}
    candidate = {r: v + 3.0 for r, v in reference.items()}

    got = ev.paired_delta(candidate, reference, 0.90)

    assert (got.mean_difference, got.low, got.high) == pytest.approx((3.0, 3.0, 3.0), rel=REL)


def test_paired_delta_single_common_replication_has_no_interval(ev: Any) -> None:
    got = ev.paired_delta({0: 5.0, 1: 2.0}, {1: 1.5}, 0.90)

    assert (got.mean_difference, got.low, got.high, got.n) == (0.5, None, None, 1)


# --- deterministic collapse (D-096) ---------------------------------------------------------


def test_deterministic_source_runs_one_replication_flagged_deterministic(ev: Any) -> None:
    generated = generate_core(0)
    candidate = ev.CandidateInput(
        plan_id="core", compiled=generated.compiled, schedule=generated.schedule
    )

    result = _evaluate(
        ev, [candidate], source=DeterministicSource(), policy=SimulationPolicy(), replications=20
    )

    assert (result.status, result.replications, result.deterministic) == ("completed", 1, True)
    (evaluation,) = result.candidates
    assert len(evaluation.per_rep) == len(evaluation.runs) == 1
    for name in metrics.METRIC_NAMES:
        assert (evaluation.metrics[name].n, evaluation.metrics[name].ci_low) == (1, None), name


def test_deterministic_collapse_applies_to_the_baseline_too(ev: Any) -> None:
    result = _evaluate(
        ev,
        _inputs(ev, PLAN_231, PLAN_123),
        source=DeterministicSource(),
        policy=SimulationPolicy(),
        replications=20,
        baseline_rule="edd",
    )

    assert (result.replications, result.deterministic) == (1, True)
    assert len(result.baseline.per_rep) == 1
    delta = result.paired["b-231"]["weighted_tardiness"]
    assert (delta.n, delta.low, delta.high) == (1, None, None)


def test_profile_source_with_only_degenerate_factors_collapses(ev: Any) -> None:
    policy = SimulationPolicy(
        distribution_source="profile",
        run_factors=(FactorRule(distribution=Deterministic(value=1.2)),),
    )

    result = _evaluate(
        ev, _inputs(ev, PLAN_123), source=ProfileSource(policy), policy=policy, replications=10
    )

    assert (result.replications, result.deterministic) == (1, True)


# --- stochastic replications -----------------------------------------------------------------


def test_seed_calibration_profile_runs_every_replication_with_spread(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_123), replications=6)

    assert (result.status, result.replications, result.deterministic) == ("completed", 6, False)
    (evaluation,) = result.candidates
    assert len(evaluation.per_rep) == len(evaluation.runs) == 6
    makespan = evaluation.metrics["makespan"]
    assert makespan.n == 6
    assert makespan.std > 0.0
    assert makespan.ci_low < makespan.mean < makespan.ci_high


def test_candidate_metrics_are_stats_of_its_per_replication_values(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123))

    for evaluation in result.candidates:
        assert set(evaluation.metrics) == set(metrics.METRIC_NAMES)
        for name in metrics.METRIC_NAMES:
            expected = _expected_stats(_metric(evaluation.per_rep, name), 0.90)
            _assert_matches(evaluation.metrics[name], expected)


def test_intervals_use_the_policy_confidence_level(ev: Any) -> None:
    policy = SEED_CALIBRATION.model_copy(update={"confidence_level": 0.8})

    result = _evaluate(ev, _inputs(ev, PLAN_123), policy=policy)

    (evaluation,) = result.candidates
    expected = _expected_stats(_metric(evaluation.per_rep, "makespan"), 0.8)
    _assert_matches(evaluation.metrics["makespan"], expected)


def test_replication_r_runs_with_rep_draws_of_the_seed_and_index_r(ev: Any) -> None:
    pid, schedule = PLAN_231
    result = _evaluate(ev, _inputs(ev, PLAN_231), replications=4, seed=SEED)

    (evaluation,) = result.candidates
    limits = engine.Limits.for_shop(PINEDO_SHOP)
    for r, (run, rep_metrics) in enumerate(zip(evaluation.runs, evaluation.per_rep, strict=True)):
        draws = engine.RepDraws(seed=SEED, replication=r, source=STOCHASTIC)
        direct = engine.execute(PINEDO_SHOP, engine.PlanControl(schedule), draws, (), limits)
        assert metrics.with_slip_causes(run.records) == metrics.with_slip_causes(direct.records)
        assert rep_metrics == metrics.replication_metrics(PINEDO, PINEDO_SHOP, run, schedule)
    assert pid == evaluation.plan_id


def test_paired_seeds_give_every_operation_the_same_run_draw_in_every_plan(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), baseline_rule="edd")

    runs = {c.plan_id: c.runs for c in result.candidates} | {"baseline": result.baseline.runs}
    for r in range(result.replications):
        drawn = {
            name: {rec.operation_id: rec.run_seconds for rec in reps[r].records}
            for name, reps in runs.items()
        }
        assert drawn["b-231"] == drawn["a-123"] == drawn["baseline"], r
    # Different replications draw differently: the pairing is not a constant factor.
    first = {rec.operation_id: rec.run_seconds for rec in runs["b-231"][0].records}
    second = {rec.operation_id: rec.run_seconds for rec in runs["b-231"][1].records}
    assert first != second


# --- budget and truncation (D-099) -----------------------------------------------------------


def test_budget_cut_mid_replication_keeps_the_last_full_replication(ev: Any) -> None:
    # start read 0; submission checks read 1..5 (< 5.5): reps 0 and 1 of both candidates and
    # rep 2 of the first; the check at 6 stops. Rep 2 is dropped for everyone.
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), budget_seconds=5.5, clock=FakeClock())

    assert (result.status, result.replications) == ("truncated", 2)
    assert [len(c.per_rep) for c in result.candidates] == [2, 2]
    assert [len(c.runs) for c in result.candidates] == [2, 2]
    assert all(c.metrics["makespan"].n == 2 for c in result.candidates)


def test_budget_cut_counts_the_baseline_and_keeps_its_count_equal(ev: Any) -> None:
    # Three tasks per replication: checks 1..7 (< 7.5) submit reps 0, 1 and one task of rep 2.
    result = _evaluate(
        ev,
        _inputs(ev, PLAN_231, PLAN_123),
        budget_seconds=7.5,
        clock=FakeClock(),
        baseline_rule="edd",
    )

    assert (result.status, result.replications) == ("truncated", 2)
    assert [len(c.per_rep) for c in result.candidates] == [2, 2]
    assert len(result.baseline.per_rep) == 2
    assert all(delta.n == 2 for delta in result.paired["a-123"].values())


def test_budget_for_exactly_one_replication_gives_one_truncated_replication(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), budget_seconds=2.5, clock=FakeClock())

    assert (result.status, result.replications) == ("truncated", 1)
    assert all(c.metrics["makespan"].ci_low is None for c in result.candidates)


@pytest.mark.parametrize("budget", [0.5, 1.5])
def test_budget_that_completes_no_replication_raises_budget_exhausted(
    ev: Any, budget: float
) -> None:
    # 0.5: nothing submitted. 1.5: rep 0 of the first candidate only.
    candidates = _inputs(ev, PLAN_231, PLAN_123)

    with pytest.raises(ev.BudgetExhausted) as raised:
        _evaluate(ev, candidates, budget_seconds=budget, clock=FakeClock())

    assert raised.value.code == "budget_exhausted"


def test_ample_budget_completes_every_replication(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), budget_seconds=100.0, clock=FakeClock())

    assert (result.status, result.replications) == ("completed", 5)


def test_budget_exactly_reached_stops_submission(ev: Any) -> None:
    # The check is strict (< budget): at budget 4.0 the read of 4 stops rep 1's second task.
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), budget_seconds=4.0, clock=FakeClock())

    assert (result.status, result.replications) == ("truncated", 1)


# --- workers (D-099) -------------------------------------------------------------------------


def test_workers_1_and_3_give_identical_results(ev: Any) -> None:
    candidates = _inputs(ev, PLAN_231, PLAN_123)

    one = _evaluate(ev, candidates, replications=3, workers=1, baseline_rule="edd")
    three = _evaluate(ev, candidates, replications=3, workers=3, baseline_rule="edd")

    assert dataclasses.replace(one, elapsed_seconds=0.0) == dataclasses.replace(
        three, elapsed_seconds=0.0
    )
    assert one.replications == 3


@pytest.mark.parametrize("source", ["stalling", "dying"])
def test_pool_stops_waiting_at_the_budget_and_terminates_its_workers(ev: Any, source: str) -> None:
    # A worker that stalls (or dies, as an OOM kill would) must not hold the call past its
    # budget: in-flight replications are dropped, the workers are terminated, and with no
    # replication finished the call raises `BudgetExhausted` instead of waiting forever.
    import multiprocessing
    import time

    from _worker_faults import POLICY, DyingSource, StallingSource

    faulty = StallingSource(POLICY) if source == "stalling" else DyingSource(POLICY)
    started = time.monotonic()

    with pytest.raises(ev.BudgetExhausted):
        _evaluate(
            ev,
            _inputs(ev, PLAN_231, PLAN_123),
            source=faulty,
            policy=POLICY,
            workers=2,
            budget_seconds=3.0,
        )

    assert time.monotonic() - started < 20.0
    assert multiprocessing.active_children() == []


# --- baseline and paired deltas (REQ-138, D-103) ----------------------------------------------


def test_baseline_deltas_are_candidate_minus_baseline_by_the_difference_ci_method(
    ev: Any,
) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), baseline_rule="edd")

    assert set(result.paired) == {"b-231", "a-123"}
    base = result.baseline.per_rep
    for evaluation in result.candidates:
        deltas = result.paired[evaluation.plan_id]
        assert set(deltas) == set(metrics.METRIC_NAMES)
        for name in metrics.METRIC_NAMES:
            candidate = dict(enumerate(_metric(evaluation.per_rep, name)))
            reference = dict(enumerate(_metric(base, name)))
            _assert_matches(deltas[name], _expected_delta(candidate, reference, 0.90))


def test_baseline_runs_the_dispatch_rule_on_the_first_candidates_problem(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), replications=3, baseline_rule="spt")

    baseline = result.baseline
    assert (baseline.plan_id, baseline.robustness, baseline.bottleneck) == (None, None, None)
    assert dict(baseline.planned) == {}
    limits = engine.Limits.for_shop(PINEDO_SHOP)
    for r, run in enumerate(baseline.runs):
        draws = engine.RepDraws(seed=SEED, replication=r, source=STOCHASTIC)
        direct = engine.execute(PINEDO_SHOP, engine.DispatchControl("spt"), draws, (), limits)
        assert metrics.with_slip_causes(run.records) == metrics.with_slip_causes(direct.records)
    assert result.baseline not in result.candidates
    assert "baseline" not in result.ranking


def test_without_a_baseline_rule_there_is_no_baseline_and_no_deltas(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123))

    assert result.baseline is None
    assert result.paired == {}


def test_identical_plans_get_identical_metrics_and_deltas(ev: Any) -> None:
    candidates = _inputs(ev, PLAN_123, ("c-123-again", PLAN_123[1]))

    result = _evaluate(ev, candidates, baseline_rule="edd")

    first, second = result.candidates
    assert first.per_rep == second.per_rep
    assert first.metrics == second.metrics
    assert result.paired["a-123"] == result.paired["c-123-again"]


# --- ranking (D-068) ---------------------------------------------------------------------------


def test_ranking_equals_objectives_rank_over_mean_objective_values(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_123, PLAN_231), baseline_rule="edd")

    means = {
        c.plan_id: {
            kind: float(np.mean([rep.objective_values[kind] for rep in c.per_rep]))
            for kind in ObjectiveKind
        }
        for c in result.candidates
    }
    expected = rank(means, PINEDO.profile.objective)
    assert list(result.ranking) == expected
    # Precondition: the fixture discriminates (not id order, not input order).
    assert expected == ["b-231", "a-123"]


def test_candidates_keep_input_order(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123))

    # Not id order: input order.
    assert [c.plan_id for c in result.candidates] == ["b-231", "a-123"]


# --- per-candidate analyses and evidence ------------------------------------------------------


def test_candidate_planned_values_robustness_bottleneck_and_evidence_rows(ev: Any) -> None:
    result = _evaluate(ev, _inputs(ev, PLAN_231, PLAN_123), replications=3)

    for evaluation in result.candidates:
        schedule = dict((PLAN_231, PLAN_123))[evaluation.plan_id]
        runs = list(evaluation.runs)
        assert dict(evaluation.planned) == dict(evaluate(PINEDO, schedule, None))
        assert evaluation.robustness == robustness.robustness_summary(
            PINEDO_SHOP, PINEDO, schedule, SEED_CALIBRATION, TARGETS
        )
        assert evaluation.bottleneck.machine_id == "M"
        assert evaluation.bottleneck.planned_utilization == pytest.approx(1.0, rel=REL)
        assert evaluation.operation_rows == metrics.operation_rows(
            evaluation.plan_id, runs, schedule
        )
        assert evaluation.job_rows == metrics.job_rows(
            evaluation.plan_id, PINEDO_SHOP, runs, schedule
        )


def test_elapsed_seconds_is_the_clock_span_of_the_evaluation(ev: Any) -> None:
    clock = FakeClock()

    result = _evaluate(ev, _inputs(ev, PLAN_123), replications=2, clock=clock)

    # Reads: start, two submissions, then the end read; elapsed = end - start.
    assert result.elapsed_seconds == pytest.approx(clock.reads - 1.0)


# --- refused inputs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "candidates"),
    [
        ({}, ()),
        ({"replications": 0}, (PLAN_123,)),
        ({"workers": 0}, (PLAN_123,)),
        ({"baseline_rule": "random"}, (PLAN_123,)),
        ({}, (PLAN_123, ("a-123", PLAN_231[1]))),
    ],
    ids=["no-candidates", "zero-replications", "zero-workers", "unknown-rule", "duplicate-id"],
)
def test_bad_inputs_raise_value_error(
    ev: Any, overrides: dict[str, Any], candidates: tuple[tuple[str, Schedule], ...]
) -> None:
    inputs = _inputs(ev, *candidates)

    with pytest.raises(ValueError):
        _evaluate(ev, inputs, **overrides)
