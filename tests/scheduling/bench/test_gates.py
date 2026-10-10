"""Tests for the HMLV generator and the D-083 gate harness (T-127, COMP-027, REQ-125).

The dispatch rules `fifo`, `edd` and `spt` are not registered yet, so the harness logic runs
on a tiny bench (3 jobs x 2 ops on 2 centers of 2 machines, no calendars) whose rules are
fakes built on the brute-force oracle: each fake rule puts every job's operations on their
first option and sequences all machines by one job key (release, due date or total run
time), so its sequences never deadlock. The oracle-best fake enumerates every option choice
and machine order and keeps the schedule with the lowest score under the objective the
harness hands it. Tests that need the real rules skip until they are registered.
"""

from __future__ import annotations

import functools
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from oracle import SequenceDeadlock, all_sequences, semi_active
from oracle_math import objective_values

from bench.gates import (
    HELD_OUT_BENCH,
    RULES,
    Bench,
    GateVerdict,
    baseline_table,
    beats_baselines,
    bench_profile,
    bench_score,
    format_baseline_table,
    judge,
    reference_values,
    score,
    seed_stable,
)
from bench.hmlv import (
    HELD_OUT_SEEDS,
    TRAINING_SEEDS,
    estimated_makespan,
    hmlv_instance,
    scale_instance,
    tiny_instance,
)
from twinflow.scheduling import is_registered_solver
from twinflow.scheduling.baseline import BaselineSolver
from twinflow.scheduling.model import (
    AbsDiffTerm,
    CompiledProblem,
    ObjectiveKind,
    ObjectiveSpec,
    Problem,
    Schedule,
    SchedulingProfile,
    WeightedSum,
)
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature, SolveContext, Solver
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate

WT = ObjectiveKind.WEIGHTED_TARDINESS
SETUP = ObjectiveKind.TOTAL_SETUP
TINY_SEEDS = (0, 1, 2)
ALL_SEEDS = TRAINING_SEEDS + HELD_OUT_SEEDS
EVERY_FEATURE = frozenset(Feature)


def _rules_registered() -> bool:
    return all(is_registered_solver(rule) for rule in RULES)


needs_rules = pytest.mark.skipif(
    not _rules_registered(), reason=f"dispatch rules {RULES} are not registered yet"
)


# --- fake methods ----------------------------------------------------------------------------


def _first_option_sequences(problem: Problem, rank: dict[str, float]) -> dict[str, list[str]]:
    """Each op on its first option; every machine orders ops by (job rank, job, op index)."""
    keyed: list[tuple[float, int, int, str, str]] = []
    for j, job in enumerate(problem.jobs):
        for k, op in enumerate(job.operations):
            keyed.append((rank[job.id], j, k, op.options[0].machine_id, op.id))
    sequences: dict[str, list[str]] = {m.id: [] for m in problem.machines}
    for *_, machine_id, op_id in sorted(keyed):
        sequences[machine_id].append(op_id)
    return sequences


def _job_rank(problem: Problem, rule: str) -> dict[str, float]:
    if rule == "fifo":
        return {job.id: job.release for job in problem.jobs}
    if rule == "edd":
        return {job.id: job.due if job.due is not None else math.inf for job in problem.jobs}
    return {
        job.id: sum(
            op.options[0].fixed_seconds + op.quantity * op.options[0].unit_seconds
            for op in job.operations
        )
        for job in problem.jobs
    }


def _schedule(name: str, assignments: Schedule) -> Schedule:
    return assignments.model_copy(update={"status": "FEASIBLE", "solver": name})


@dataclass(frozen=True)
class FakeRule:
    """A permutation dispatch rule on the oracle: fifo, edd or spt by job key."""

    rule: str

    @property
    def name(self) -> str:
        return f"fake_{self.rule}"

    @property
    def capabilities(self) -> frozenset[Feature]:
        return EVERY_FEATURE

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        sequences = _first_option_sequences(problem, _job_rank(problem, self.rule))
        return _schedule(self.name, semi_active(problem, sequences))


def _spec_score(spec: ObjectiveSpec, values: dict[ObjectiveKind, float]) -> float:
    assert isinstance(spec, WeightedSum), f"the harness must hand a weighted sum, got {spec}"
    return math.fsum(term.weight * values[term.kind] / (term.scale or 1.0) for term in spec.terms)


@dataclass(frozen=True)
class OracleBest:
    """Every option choice and machine order; the lowest score under the given objective."""

    @property
    def name(self) -> str:
        return "fake_oracle_best"

    @property
    def capabilities(self) -> frozenset[Feature]:
        return EVERY_FEATURE

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        compiled = CompiledProblem(problem=problem, profile=SchedulingProfile())
        best: tuple[float, Schedule] | None = None
        for sequences in all_sequences(problem):
            try:
                schedule = semi_active(problem, sequences)
            except SequenceDeadlock:
                continue
            value = _spec_score(objective, objective_values(compiled, schedule))
            if best is None or value < best[0]:
                best = (value, schedule)
        assert best is not None
        return _schedule(self.name, best[1])


@dataclass(frozen=True)
class Scripted:
    """Answers with `answer(problem, context)`; records nothing, so it stays hashable."""

    answer: Callable[[Problem, ObjectiveSpec, SolveContext], Schedule]
    label: str = "fake_scripted"

    @property
    def name(self) -> str:
        return self.label

    @property
    def capabilities(self) -> frozenset[Feature]:
        return EVERY_FEATURE

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        return self.answer(problem, objective, context)


def _factory(solver: Solver) -> Callable[[], Solver]:
    return functools.partial(lambda s: s, solver)


TINY_RULES = tuple((rule, _factory(FakeRule(rule))) for rule in RULES)
TINY_BENCH = Bench(instance=tiny_instance, instance_seeds=TINY_SEEDS, rules=TINY_RULES)


def _edd_answer(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
    return FakeRule("edd").solve(problem, objective=objective, budget_seconds=1.0, context=context)


def _unknown(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
    return Schedule(status="UNKNOWN", assignments=(), solver="fake_unknown", elapsed_seconds=0.0)


def _raises(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
    raise RuntimeError("boom")


def _empty_feasible(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
    return Schedule(status="FEASIBLE", assignments=(), solver="fake_empty", elapsed_seconds=0.0)


def _seeded_order(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
    """EDD on even seeds, SPT on odd ones: valid plans whose output depends on the seed."""
    rule = FakeRule("edd" if context.seed % 2 == 0 else "spt")
    return rule.solve(problem, objective=objective, budget_seconds=1.0, context=context)


EDD_COPY = _factory(Scripted(_edd_answer, "fake_edd_copy"))
ORACLE_BEST = _factory(OracleBest())


# --- the generator ---------------------------------------------------------------------------


def test_seed_sets_are_the_d083_training_and_held_out_ranges() -> None:
    assert TRAINING_SEEDS == tuple(range(20))
    assert HELD_OUT_SEEDS == tuple(range(100, 130))


@pytest.mark.parametrize("seed", (0, 7, 100, 129))
def test_hmlv_instance_is_deterministic_per_seed(seed: int) -> None:
    assert hmlv_instance(seed) == hmlv_instance(seed), f"seed {seed}"


def test_hmlv_instances_differ_across_seeds() -> None:
    problems = {hmlv_instance(seed).problem.model_dump_json() for seed in (0, 1, 2)}
    assert len(problems) == 3


def test_hmlv_default_shape_is_40_jobs_by_5_ops_on_4_centers() -> None:
    problem = hmlv_instance(0).problem

    assert len(problem.jobs) == 40
    assert all(len(job.operations) == 5 for job in problem.jobs)
    assert {wc.id for wc in problem.work_centers} == {f"C{c}" for c in range(4)}
    for job in problem.jobs:
        ops = job.operations
        assert ops[0].predecessors == ()
        for prev, op in zip(ops, ops[1:], strict=False):
            assert [p.operation_id for p in op.predecessors] == [prev.id], job.id


def test_hmlv_options_have_one_to_three_machines_of_their_center_with_speed_spread() -> None:
    problem = hmlv_instance(3).problem
    center_of = {m.id: m.work_center_id for m in problem.machines}
    counts: Counter[int] = Counter()
    for job in problem.jobs:
        for op in job.operations:
            counts[len(op.options)] += 1
            assert {center_of[o.machine_id] for o in op.options} == {op.work_center_id}
            assert all(o.preference_cost in (0.0, 1.0, 2.0, 3.0) for o in op.options)
            runs = [o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options]
            # one base time divided by speeds in [0.8, 1.25]: the ratio is at most 1.5625
            assert max(runs) / min(runs) <= 1.25 / 0.8 + 1e-6, op.id
    assert set(counts) == {1, 2, 3}, counts


def test_hmlv_weight_classes_follow_60_30_10_over_all_seeds() -> None:
    classes = Counter(
        job.weight_class for seed in ALL_SEEDS for job in hmlv_instance(seed).problem.jobs
    )
    total = sum(classes.values())
    shares = {wc: classes[wc] / total for wc in (1, 2, 4)}
    assert set(classes) == {1, 2, 4}
    assert abs(shares[1] - 0.6) < 0.04 and abs(shares[2] - 0.3) < 0.04, shares
    assert abs(shares[4] - 0.1) < 0.03, shares


def test_hmlv_calendars_cover_exactly_half_the_machines() -> None:
    problem = hmlv_instance(5).problem
    calendared = [m for m in problem.machines if m.calendar_id is not None]
    assert len(calendared) == len(problem.machines) // 2
    assert not any(m.calendar_id for m in tiny_instance(0).problem.machines)


def test_hmlv_setups_are_wire_size_abs_diff_plus_a_family_matrix() -> None:
    problem = hmlv_instance(0).problem
    (function,) = problem.setup_functions
    assert [type(term) for term in function.terms] == [AbsDiffTerm]
    assert function.terms[0].param == "wire"
    assert function.family_matrix, "no family matrix"
    assert all(wc.setup_function_id == function.id for wc in problem.work_centers)
    ops = [op for job in problem.jobs for op in job.operations]
    assert all("wire" in op.setup_params and op.setup_family for op in ops)


def test_hmlv_zero_setup_severity_gives_zero_setup_seconds() -> None:
    (function,) = hmlv_instance(0, setup_severity=0.0).problem.setup_functions
    assert all(term.seconds_per_unit == 0.0 for term in function.terms)
    assert all(entry.seconds == 0.0 for entry in function.family_matrix)


@pytest.mark.parametrize(("tau", "spread"), [(0.5, 0.5), (0.2, 0.8), (0.8, 0.2)])
def test_hmlv_due_dates_follow_pinedos_tau_and_r_window(tau: float, spread: float) -> None:
    problem = hmlv_instance(11, tightness=tau, due_range=spread).problem
    cmax = estimated_makespan(problem, setup_severity=0.5)
    low, high = cmax * (1 - tau - spread / 2), cmax * (1 - tau + spread / 2)
    dues = [job.due for job in problem.jobs]
    assert all(d is not None and low - 1e-6 <= d <= high + 1e-6 for d in dues), (low, high)
    assert max(d for d in dues if d is not None) - min(d for d in dues if d is not None) > 0


def test_hmlv_tighter_tau_gives_earlier_due_dates() -> None:
    def mean_due(tau: float) -> float:
        dues = [job.due or 0.0 for job in hmlv_instance(4, tightness=tau).problem.jobs]
        return sum(dues) / len(dues)

    assert mean_due(0.8) < mean_due(0.5) < mean_due(0.2)


def test_hmlv_flow_line_visits_centers_in_the_same_order() -> None:
    problem = hmlv_instance(2, ops_per_job=4, centers=4, flow_line=True).problem
    routes = {tuple(op.work_center_id for op in job.operations) for job in problem.jobs}
    assert routes == {("C0", "C1", "C2", "C3")}


def test_hmlv_job_shop_routes_differ_between_jobs() -> None:
    problem = hmlv_instance(2).problem
    routes = {tuple(op.work_center_id for op in job.operations) for job in problem.jobs}
    assert len(routes) > 1


def test_hmlv_flow_line_with_more_ops_than_centers_is_rejected() -> None:
    with pytest.raises(ValueError, match="flow_line"):
        hmlv_instance(0, ops_per_job=5, centers=4, flow_line=True)


@pytest.mark.slow
@pytest.mark.parametrize("seed", ALL_SEEDS)
def test_every_training_and_held_out_instance_validates_and_the_baseline_verifies(
    seed: int,
) -> None:
    request = hmlv_instance(seed)
    assert validate(request) == [], f"seed {seed}"
    result = solve(request, solver=BaselineSolver())
    assert result.verified, f"seed {seed}: {result.issues}"


@pytest.mark.parametrize("seed", TINY_SEEDS)
def test_tiny_instances_validate_and_the_baseline_verifies(seed: int) -> None:
    request = tiny_instance(seed)
    assert validate(request) == [], f"seed {seed}"
    assert solve(request, solver=BaselineSolver()).verified, f"seed {seed}"


@pytest.mark.slow
def test_scale_instance_is_600_jobs_by_8_ops_and_validates() -> None:
    request = scale_instance(0)
    assert len(request.problem.jobs) == 600
    assert all(len(job.operations) == 8 for job in request.problem.jobs)
    assert validate(request) == []


# --- the profile and the score ---------------------------------------------------------------


def test_bench_profile_weights_tardiness_1_and_setup_0_2_scaled_by_the_reference() -> None:
    spec = bench_profile({WT: 250.0, SETUP: 40.0})
    assert [(t.kind, t.weight, t.scale) for t in spec.terms] == [
        (WT, 1.0, 250.0),
        (SETUP, 0.2, 40.0),
    ]


def test_bench_profile_scales_a_zero_or_small_reference_value_by_one() -> None:
    spec = bench_profile({WT: 0.0, SETUP: 0.5})
    assert [t.scale for t in spec.terms] == [1.0, 1.0]
    assert [t.scale for t in bench_profile(None).terms] == [1.0, 1.0]


def test_bench_score_is_the_weighted_normalized_sum() -> None:
    spec = bench_profile({WT: 200.0, SETUP: 50.0})
    assert bench_score(spec, {WT: 100.0, SETUP: 100.0}) == pytest.approx(0.5 + 0.4)


@pytest.mark.parametrize("seed", TINY_SEEDS)
def test_the_reference_rule_scores_1_2_on_its_own_instance(seed: int) -> None:
    values = reference_values(TINY_BENCH, seed)
    if values[WT] < 1 or values[SETUP] < 1:
        pytest.skip(f"tiny seed {seed} has a reference value below 1: {values}")
    assert score(TINY_RULES[1][1], seed, bench=TINY_BENCH) == pytest.approx(1.2)


def test_score_of_an_unverified_answer_is_infinite() -> None:
    for answer in (_unknown, _raises, _empty_feasible):
        method = _factory(Scripted(answer, f"fake_{answer.__name__.strip('_')}"))
        assert score(method, 0, bench=TINY_BENCH) == math.inf, answer.__name__


def test_score_hands_the_method_the_bench_profile_scaled_by_the_reference() -> None:
    seen: list[ObjectiveSpec] = []

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seen.append(objective)
        return _edd_answer(problem, objective, context)

    score(_factory(Scripted(record, "fake_recorder")), 1, bench=TINY_BENCH)

    assert seen == [bench_profile(reference_values(TINY_BENCH, 1))]


def test_score_passes_the_seed_to_the_method() -> None:
    seeds: list[int] = []

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seeds.append(context.seed)
        return _edd_answer(problem, objective, context)

    score(_factory(Scripted(record, "fake_seed_recorder")), 0, 3, bench=TINY_BENCH)
    assert seeds == [3]


def test_score_uses_the_bench_budget() -> None:
    budgets: list[float] = []

    class Budgeted(Scripted):
        def solve(
            self,
            problem: Problem,
            *,
            objective: ObjectiveSpec,
            budget_seconds: float,
            context: SolveContext,
        ) -> Schedule:
            budgets.append(budget_seconds)
            return super().solve(
                problem, objective=objective, budget_seconds=budget_seconds, context=context
            )

    bench = Bench(
        instance=tiny_instance, instance_seeds=TINY_SEEDS, rules=TINY_RULES, budget_seconds=2.5
    )
    score(_factory(Budgeted(_edd_answer, "fake_budget")), 0, bench=bench)
    assert budgets == [2.5]


def test_an_unverified_reference_rule_is_a_bench_error() -> None:
    rules = (("fifo", TINY_RULES[0][1]), ("edd", _factory(Scripted(_unknown))), TINY_RULES[2])
    bench = Bench(instance=tiny_instance, instance_seeds=TINY_SEEDS, rules=rules)
    with pytest.raises(RuntimeError, match="reference rule edd did not verify"):
        reference_values(bench, 0)


# --- the verdict -----------------------------------------------------------------------------

TABLE = {"fifo": (2.0, 2.0, 2.0), "edd": (1.2, 1.2, 1.2), "spt": (1.5, 1.0, 3.0)}


def test_judge_passes_a_method_below_every_mean_winning_enough_instances() -> None:
    verdict = judge([(1.0, 1.0, 1.1)], TABLE, required_wins=2)
    assert verdict.passed, verdict.failures
    assert verdict.wins == (2,)
    assert verdict.seed_means == pytest.approx((3.1 / 3,))


def test_judge_fails_a_method_with_too_few_wins_despite_a_low_mean() -> None:
    verdict = judge([(0.1, 1.0, 1.5)], TABLE, required_wins=2)
    assert not verdict.passed
    assert verdict.wins == (1,)
    assert any("wins" in failure for failure in verdict.failures), verdict.failures


def test_judge_fails_a_method_whose_mean_is_not_below_one_baseline() -> None:
    # beats the per-instance best twice but its mean equals EDD's mean 1.2
    verdict = judge([(1.1, 0.9, 1.6)], TABLE, required_wins=2)
    assert not verdict.passed
    assert any("edd" in failure for failure in verdict.failures), verdict.failures


def test_judge_counts_a_tie_with_the_best_rule_as_no_win() -> None:
    verdict = judge([(1.2, 1.0, 1.2)], TABLE, required_wins=1)
    assert verdict.wins == (0,)
    assert not verdict.passed


def test_judge_requires_every_seed_to_pass() -> None:
    verdict = judge([(1.0, 1.0, 1.1), (1.0, 1.0, 1.5)], TABLE, required_wins=2)
    assert verdict.wins == (2, 1)
    assert not verdict.passed


def test_judge_fails_a_coefficient_of_variation_above_0_05() -> None:
    stable = judge([(1.0, 1.0, 1.1), (1.0, 1.0, 1.1)], TABLE, required_wins=2)
    unstable = judge([(1.0, 1.0, 1.1), (0.5, 0.5, 0.5)], TABLE, required_wins=2)
    assert stable.passed and stable.cv == 0.0
    assert not unstable.passed
    assert unstable.cv == pytest.approx((3.1 / 3 - 0.5) / (3.1 / 3 + 0.5))
    assert any("variation" in failure for failure in unstable.failures)


def test_judge_fails_an_infinite_score() -> None:
    verdict = judge([(1.0, 0.9, math.inf)], TABLE, required_wins=2)
    assert not verdict.passed
    assert verdict.seed_means == (math.inf,)


def test_the_held_out_bench_needs_18_wins_of_30_and_the_tiny_bench_2_of_3() -> None:
    assert HELD_OUT_BENCH.instance_seeds == HELD_OUT_SEEDS
    assert HELD_OUT_BENCH.required_wins == 18
    assert TINY_BENCH.required_wins == 2


# --- beats_baselines on fakes ----------------------------------------------------------------


def test_a_method_returning_the_edd_schedule_does_not_beat_the_baselines() -> None:
    verdict = beats_baselines(EDD_COPY, bench=TINY_BENCH)
    assert isinstance(verdict, GateVerdict)
    assert not verdict.passed
    assert verdict.wins == (0,)


def test_the_oracle_best_method_beats_the_baselines_on_the_tiny_bench() -> None:
    verdict = beats_baselines(ORACLE_BEST, bench=TINY_BENCH)
    assert verdict.passed, (verdict.failures, baseline_table(TINY_BENCH), verdict.seed_means)


def test_beats_baselines_runs_every_requested_seed() -> None:
    verdict = beats_baselines(ORACLE_BEST, seeds=(0, 1), bench=TINY_BENCH)
    assert len(verdict.seed_means) == 2 and verdict.passed and verdict.cv == 0.0


def test_beats_baselines_fails_a_method_that_never_verifies() -> None:
    verdict = beats_baselines(_factory(Scripted(_unknown, "fake_never")), bench=TINY_BENCH)
    assert not verdict.passed
    assert verdict.seed_means == (math.inf,)


def test_the_baseline_table_is_computed_once_per_session() -> None:
    first = baseline_table(TINY_BENCH)
    assert baseline_table(TINY_BENCH) is first
    assert set(first) == set(RULES)
    assert all(len(scores) == len(TINY_SEEDS) for scores in first.values())


def test_the_baseline_table_prints_one_row_per_instance_and_the_means() -> None:
    text = format_baseline_table(TINY_BENCH)
    lines = text.splitlines()
    assert all(rule in lines[0] for rule in RULES)
    assert len(lines) == 1 + len(TINY_SEEDS) + 1
    assert lines[-1].startswith("mean")


# --- seed stability --------------------------------------------------------------------------


def test_a_deterministic_method_is_seed_stable() -> None:
    assert seed_stable(EDD_COPY, bench=TINY_BENCH)


def test_a_method_whose_output_depends_on_the_seed_is_not_seed_stable() -> None:
    assert not seed_stable(_factory(Scripted(_seeded_order, "fake_seeded")), bench=TINY_BENCH)


# --- the real dispatch rules -----------------------------------------------------------------


@pytest.mark.slow
@needs_rules
def test_print_the_fifo_edd_spt_table_over_the_held_out_instances() -> None:
    print("\nFIFO / EDD / SPT bench scores over the held-out HMLV instances (D-083)")
    print(format_baseline_table(HELD_OUT_BENCH))
    table = baseline_table(HELD_OUT_BENCH)
    assert all(math.isfinite(s) for scores in table.values() for s in scores), table
    # the reference scales are max(value, 1) >= value, so EDD scores at most 1.2 on each
    assert all(s <= 1.2 + 1e-9 for s in table["edd"]), table["edd"]


@pytest.mark.slow
@needs_rules
def test_a_method_returning_the_real_edd_schedule_does_not_beat_the_held_out_baselines() -> None:
    def edd(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        solver = get_solver("edd")
        return solver.solve(problem, objective=objective, budget_seconds=10.0, context=context)

    verdict = beats_baselines(_factory(Scripted(edd, "fake_real_edd")))
    assert not verdict.passed
    assert verdict.wins == (0,)
