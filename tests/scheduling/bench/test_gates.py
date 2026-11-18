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

import dataclasses
import functools
import importlib.util
import math
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from oracle import SequenceDeadlock, all_sequences, semi_active
from oracle_math import objective_values

from bench import gates
from bench.gates import (
    BENCH_PORTFOLIO,
    FLOW_LINE_BENCH,
    GATE_SEEDS,
    HELD_OUT_BENCH,
    MAKESPAN_BENCH,
    MAKESPAN_TERMS,
    MAX_CV,
    RULES,
    Bench,
    GateVerdict,
    baseline_table,
    beats_baselines,
    bench_profile,
    bench_score,
    format_baseline_table,
    format_verdict,
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
from twinflow.scheduling import available_solvers, is_registered_solver, solve_portfolio
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
needs_ortools = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None, reason="ortools (scheduling extra) is absent"
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


# --- method params and the makespan bench (harness extension for T-133, T-143) ----------------


def test_params_reach_the_solver_as_its_context_params() -> None:
    seen: list[dict[str, object]] = []

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seen.append(dict(context.params))
        return _edd_answer(problem, objective, context)

    method = _factory(Scripted(record, "fake_params"))
    score(method, 0, bench=TINY_BENCH, params={"flexibility": True, "K1": 0.5})
    score(method, 0, bench=TINY_BENCH)

    assert seen == [{"flexibility": True, "K1": 0.5}, {}]


def test_beats_baselines_and_seed_stable_pass_params_on_every_run() -> None:
    seen: list[dict[str, object]] = []

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seen.append(dict(context.params))
        return _edd_answer(problem, objective, context)

    method = _factory(Scripted(record, "fake_params_all"))
    beats_baselines(method, bench=TINY_BENCH, params={"mode": "active"})
    seed_stable(method, bench=TINY_BENCH, params={"mode": "active"})

    # seed 0 on every instance, then seed 1 on the stability instances (seed 0 is reused)
    assert len(seen) == len(TINY_SEEDS) + len(TINY_SEEDS)
    assert all(params == {"mode": "active"} for params in seen), seen


def test_the_makespan_bench_scores_makespan_normalized_by_the_reference_rule() -> None:
    bench = dataclasses.replace(TINY_BENCH, terms=MAKESPAN_TERMS)
    makespan = reference_values(bench, 0)[ObjectiveKind.MAKESPAN]
    assert makespan >= 1.0

    assert score(TINY_RULES[1][1], 0, bench=bench) == pytest.approx(1.0)
    assert bench_profile(reference_values(bench, 0), bench.terms).terms[0].scale == makespan
    assert MAKESPAN_BENCH.instance_seeds == HELD_OUT_SEEDS
    assert MAKESPAN_BENCH.terms == ((ObjectiveKind.MAKESPAN, 1.0),)


def test_the_edd_copy_scores_1_on_the_makespan_bench_and_wins_nothing() -> None:
    bench = dataclasses.replace(TINY_BENCH, terms=MAKESPAN_TERMS)
    verdict = beats_baselines(EDD_COPY, bench=bench)
    assert verdict.seed_means == pytest.approx((1.0,))
    assert verdict.wins == (0,) and not verdict.passed


def test_the_makespan_bench_hands_the_method_a_makespan_objective() -> None:
    seen: list[ObjectiveSpec] = []

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seen.append(objective)
        return _edd_answer(problem, objective, context)

    bench = dataclasses.replace(TINY_BENCH, terms=MAKESPAN_TERMS)
    score(_factory(Scripted(record, "fake_makespan_recorder")), 2, bench=bench)
    (spec,) = seen
    assert isinstance(spec, WeightedSum)
    assert [(t.kind, t.weight) for t in spec.terms] == [(ObjectiveKind.MAKESPAN, 1.0)]


def _recorder(label: str, seen: list[tuple[int, int]]) -> Callable[[], Solver]:
    """A deterministic EDD copy that records (instance size marker, seed) per run."""

    def record(problem: Problem, objective: ObjectiveSpec, context: SolveContext) -> Schedule:
        seen.append((len(problem.jobs), context.seed))
        return _edd_answer(problem, objective, context)

    return _factory(Scripted(record, label))


# --- session run cache, portfolio method, D-083 seed policy and the table (T-166) -----------


def test_each_instance_run_happens_once_per_session_and_is_reused() -> None:
    seen: list[tuple[int, int]] = []
    method = _recorder("fake_cached", seen)

    first = beats_baselines(method, bench=TINY_BENCH)
    again = beats_baselines(method, bench=TINY_BENCH)
    assert seed_stable(method, bench=TINY_BENCH)
    score(method, TINY_SEEDS[0], bench=TINY_BENCH)

    assert again == first
    # seed 0 on every instance once; seed 1 only for the stability instances
    assert sorted(s for _, s in seen) == [0] * len(TINY_SEEDS) + [1] * len(TINY_SEEDS)


def test_the_run_cache_keys_on_params_seed_and_bench() -> None:
    seen: list[tuple[int, int]] = []
    method = _recorder("fake_keyed", seen)
    other_bench = dataclasses.replace(TINY_BENCH, budget_seconds=3.0)

    score(method, 0, bench=TINY_BENCH)
    score(method, 0, bench=TINY_BENCH, params={"mode": "a"})
    score(method, 0, bench=TINY_BENCH, params={"mode": "b"})
    score(method, 0, 2, bench=TINY_BENCH)
    score(method, 0, bench=other_bench)
    score(method, 0, bench=TINY_BENCH, params={"mode": "a"})

    assert len(seen) == 5


def test_portfolio_method_scores_the_recommended_candidate() -> None:
    portfolio = gates.Portfolio(("edd", "atcs"))
    spec = bench_profile(reference_values(TINY_BENCH, 0))
    request = tiny_instance(0)
    request = request.model_copy(
        update={"profile": request.profile.model_copy(update={"objective": spec})}
    )
    direct = solve_portfolio(
        request, budget_seconds=TINY_BENCH.budget_seconds, seed=0, pipelines=portfolio.pipelines
    )
    recommended = next(c for c in direct.candidates if c.id == direct.recommended)

    got = score(portfolio, 0, bench=TINY_BENCH)

    assert got == pytest.approx(bench_score(spec, recommended.objective_values))
    assert got <= min(score("edd", 0, bench=TINY_BENCH), score("atcs", 0, bench=TINY_BENCH))


def test_portfolio_without_a_verified_candidate_scores_infinity() -> None:
    assert score(gates.Portfolio(("no_such_solver",)), 0, bench=TINY_BENCH) == math.inf


def test_portfolio_runs_with_its_worker_count() -> None:
    assert gates.Portfolio(("edd",)).workers == 1
    assert gates.Portfolio(("edd",), workers=2).label == "portfolio"


@pytest.mark.parametrize(
    ("method", "deterministic"),
    [
        ("atcs", True),
        ("tabu", True),
        ("sa", False),
    ],
)
def test_d083_seed_policy_follows_the_registry_deterministic_flag(
    method: str, deterministic: bool
) -> None:
    assert gates.is_deterministic(method) is deterministic
    assert gates.gate_seeds(method) == ((0,) if deterministic else GATE_SEEDS)


def test_a_portfolio_is_stochastic_with_a_stochastic_stage_or_several_workers() -> None:
    assert not gates.is_deterministic(gates.Portfolio(("atcs", "atcs->sa")))
    assert not gates.is_deterministic(gates.Portfolio(("atcs",), workers=2))
    assert gates.is_deterministic(gates.Portfolio(("atcs", "atcs->tabu", "edd")))
    assert gates.gate_seeds(gates.Portfolio(("atcs", "atcs->sa"))) == GATE_SEEDS


def test_method_family_is_the_registry_family_or_portfolio() -> None:
    assert gates.method_family("atcs") == "composite"
    assert gates.method_family("gt_active") == "construction"
    assert gates.method_family("sa") == "improvement"
    assert gates.method_family(gates.Portfolio(("atcs",))) == "portfolio"


def test_gate_report_of_a_deterministic_method_runs_seed_0_and_the_stability_check() -> None:
    case = gates.GateCase("oracle", ORACLE_BEST, bench=TINY_BENCH, deterministic=True)
    report = gates.gate_report(case)
    assert report.verdict.passed and report.verdict.seed_means and len(report.verdict.wins) == 1
    assert report.stable is True
    assert report.passed


def test_gate_report_fails_a_deterministic_method_whose_output_depends_on_the_seed() -> None:
    method = _factory(Scripted(_seeded_order, "fake_seeded_report"))
    case = gates.GateCase("seeded", method, bench=TINY_BENCH, deterministic=True)
    report = gates.gate_report(case)
    assert report.stable is False
    assert not report.passed


def test_gate_report_of_a_stochastic_method_runs_seeds_0_to_4_and_skips_the_identity_check() -> (
    None
):
    seen: list[tuple[int, int]] = []
    case = gates.GateCase(
        "stochastic", _recorder("fake_stochastic", seen), bench=TINY_BENCH, deterministic=False
    )
    report = gates.gate_report(case)
    assert len(report.verdict.seed_means) == len(GATE_SEEDS)
    assert report.stable is None
    assert sorted({s for _, s in seen}) == list(GATE_SEEDS)


def test_gate_report_is_computed_once_per_case() -> None:
    case = gates.GateCase("oracle-once", ORACLE_BEST, bench=TINY_BENCH, deterministic=True)
    assert gates.gate_report(case) is gates.gate_report(case)


def test_gate_table_has_one_row_per_method_and_reference_rows_marked_not_gated() -> None:
    passing = gates.gate_report(
        gates.GateCase("oracle", ORACLE_BEST, bench=TINY_BENCH, deterministic=True)
    )
    failing = gates.gate_report(
        gates.GateCase("edd-copy", EDD_COPY, bench=TINY_BENCH, deterministic=True)
    )
    reference = gates.reference_row("fifo", TINY_RULES[0][1], bench=TINY_BENCH)

    text = gates.format_gate_table([passing, failing], [reference])
    lines = text.splitlines()

    header = lines[0].split()
    assert header[:2] == ["method", "family"]
    assert {"mean", "wins", "cv", "verdict"} <= set(header)
    oracle_row = next(line for line in lines if line.startswith("oracle"))
    edd_row = next(line for line in lines if line.startswith("edd-copy"))
    fifo_row = next(line for line in lines if line.startswith("fifo"))
    assert oracle_row.endswith("PASS") and "/3" in oracle_row
    assert edd_row.endswith("FAIL") and "0/3" in edd_row
    assert fifo_row.endswith("reference")


def test_format_verdict_names_the_method_mean_wins_and_cv() -> None:
    verdict = judge([(1.0, 1.0, 1.1)], TABLE, required_wins=2)
    row = format_verdict("atcs", verdict, 3)
    assert row.startswith("atcs")
    assert "1.0333" in row and "2/3" in row and "cv 0.0000" in row and row.endswith("PASS")


# --- the gate table (T-166; T-133, T-139, T-143, T-147..T-152; REQ-125, D-083) ---------------
#
# Every gated family runs on the 30 held-out instances once per session (`gates.gate_report`
# is cached and so is every run): the gate verdict, the seed check and the printed table read
# the same runs. D-083: a method registered deterministic runs seed 0 plus the identical-output
# check for seeds 0 and 1 on 3 instances; a stochastic one (sa) runs seeds 0-4 with CV <= 0.05.
# Anytime methods get the smallest per-instance budget that passed reliably on the TRAINING
# seeds (measured, T-166): beam (rollout limit 60, at most 0.52 s), tabu (iteration limit
# 30), rolling_horizon (atcs->tabu windows), CP-SAT at 3 s and the portfolio finish before
# their deadline, so their output does not depend on the clock.

ATCS_EXTENDED = (("flexibility", True), ("suitability", True), ("macro_jobs", True))
RULE_ATCS = (("rule", "atcs"),)
CPSAT_BENCH = dataclasses.replace(HELD_OUT_BENCH, budget_seconds=3.0)
TABU_BENCH = dataclasses.replace(HELD_OUT_BENCH, budget_seconds=2.0)
SA_BENCH = dataclasses.replace(HELD_OUT_BENCH, budget_seconds=0.5)
BEAM_BENCH = dataclasses.replace(HELD_OUT_BENCH, budget_seconds=1.0)
PORTFOLIO_BENCH = dataclasses.replace(HELD_OUT_BENCH, budget_seconds=30.0)

GateCase = gates.GateCase
GATE_CASES = (
    # composite
    pytest.param(GateCase("atc", "atc"), id="atc"),
    pytest.param(GateCase("atcs", "atcs"), id="atcs"),
    pytest.param(GateCase("atcs+flex+suit+macro", "atcs", ATCS_EXTENDED), id="atcs-extended"),
    # construction (rule atcs, DEV-23)
    pytest.param(GateCase("gt_active (rule atcs)", "gt_active", RULE_ATCS), id="gt_active"),
    pytest.param(
        GateCase("forward_load (rule atcs)", "forward_load", RULE_ATCS), id="forward_load"
    ),
    pytest.param(
        GateCase("backward_load (rule atcs)", "backward_load", RULE_ATCS), id="backward_load"
    ),
    # decomposition
    pytest.param(GateCase("sb_wt", "sb_wt"), id="sb_wt"),
    # sb_makespan optimizes makespan (Pinedo Alg 7.2.1): gated on the makespan bench (DEV-17)
    pytest.param(
        GateCase("sb_makespan (makespan bench)", "sb_makespan", bench=MAKESPAN_BENCH),
        id="sb_makespan",
    ),
    pytest.param(
        GateCase("five_phase (flow-line bench)", "five_phase", bench=FLOW_LINE_BENCH),
        id="five_phase",
    ),
    pytest.param(GateCase("rolling_horizon", "rolling_horizon"), id="rolling_horizon"),
    # exact
    pytest.param(
        GateCase("ortools (3 s)", "ortools", bench=CPSAT_BENCH), id="ortools", marks=needs_ortools
    ),
    # improvement
    pytest.param(GateCase("tabu (2 s)", "tabu", bench=TABU_BENCH), id="tabu"),
    pytest.param(GateCase("sa (0.5 s, seeds 0-4)", "sa", bench=SA_BENCH), id="sa"),
    pytest.param(GateCase("beam (1 s)", "beam", bench=BEAM_BENCH), id="beam"),
    # portfolio
    pytest.param(
        GateCase("portfolio (30 s)", BENCH_PORTFOLIO, bench=PORTFOLIO_BENCH), id="portfolio"
    ),
)
"""A later method adds one `pytest.param(GateCase(...))` row."""

GATED_FAMILIES = {"composite", "construction", "decomposition", "exact", "improvement", "portfolio"}
UNGATED_FAMILIES = {"dispatch", "baseline", "reactive"}
"""Elementary rules are the comparison set (reference rows); the baseline is the TF-002
fallback; reactive methods (rush_insert, reschedule) need a previous plan, which the bench
instances do not have: they are timed in `test_runtime.py` and reached through the portfolio
API."""


def _cases() -> list[gates.GateCase]:
    has_ortools = importlib.util.find_spec("ortools") is not None
    return [p.values[0] for p in GATE_CASES if has_ortools or p.id != "ortools"]  # type: ignore[misc]  # ParameterSet.values is a tuple of objects


def _report(case: gates.GateCase) -> gates.GateReport:
    report = gates.gate_report(case)
    print("\n" + gates.format_gate_table([report], []).splitlines()[1])
    return report


@pytest.mark.slow
@needs_rules
@pytest.mark.parametrize("case", GATE_CASES)
def test_method_beats_fifo_edd_and_spt_on_the_held_out_bench(case: gates.GateCase) -> None:
    verdict = _report(case).verdict
    assert verdict.passed, (case.label, verdict.failures, verdict.rule_means)


@pytest.mark.slow
@needs_rules
@pytest.mark.parametrize("case", GATE_CASES)
def test_method_meets_the_d083_seed_check(case: gates.GateCase) -> None:
    report = _report(case)
    if report.stable is None:
        assert len(report.verdict.seed_means) == len(GATE_SEEDS), case.label
        assert report.verdict.cv <= MAX_CV, (case.label, report.verdict.cv)
    else:
        assert report.stable, f"{case.label}: seeds 0 and 1 give different output"


def test_every_registered_method_outside_the_exempt_families_has_a_gate_row() -> None:
    gated = {case.method for case in _cases()}
    for info in available_solvers():
        if info.family in UNGATED_FAMILIES:
            continue
        if info.name == "ortools" and importlib.util.find_spec("ortools") is None:
            continue
        assert info.family in GATED_FAMILIES, info
        assert info.name in gated, f"{info.name} ({info.family}) has no gate row"


def test_the_bench_portfolio_is_deterministic_and_registry_named() -> None:
    names = {info.name for info in available_solvers()}
    stages = {s for pipeline in BENCH_PORTFOLIO.pipelines for s in pipeline.split("->")}
    assert stages <= names, stages - names
    assert gates.is_deterministic(BENCH_PORTFOLIO)
    assert BENCH_PORTFOLIO.workers == 1


@pytest.mark.slow
@needs_rules
def test_print_the_gate_table_over_every_gated_family() -> None:
    reports = [gates.gate_report(case) for case in _cases()]
    rules = [info.name for info in available_solvers() if info.family == "dispatch"]
    references = [gates.reference_row(rule, rule) for rule in rules]
    print("\nD-083 gate table: 30 held-out HMLV instances, wins vs best of FIFO/EDD/SPT")
    print(gates.format_gate_table(reports, references))
    assert {r.family for r in reports} == GATED_FAMILIES
    failed = [(r.case.label, r.verdict.failures, r.stable) for r in reports if not r.passed]
    assert not failed, failed
