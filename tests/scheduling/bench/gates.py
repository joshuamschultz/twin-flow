"""The D-083 quality gates over HMLV instances (T-127, COMP-027, REQ-125, D-083).

Score: a method's schedule on one instance is scored by the bench profile, the weighted sum
weighted_tardiness x 1 + total_setup x 0.2 with each term divided by the reference rule's
(EDD's) value on that instance. A reference value below 1 (a zero included) divides by 1
instead, so the scale is max(value, 1); EDD therefore scores 1.2 on an instance where both
of its values are at least 1, and at most 1.2 everywhere. An answer that does not verify
scores infinity.

`beats_baselines(method, seeds)`: for each seed, the method's mean score over the bench
instances is below the mean of each rule (FIFO, EDD, SPT), and its score is strictly below
the best of the three on at least 60 percent of the instances (18 of the 30 held-out); the
coefficient of variation of the per-seed means is at most 0.05. A deterministic method runs
with `seeds=(0,)`; `seed_stable` then checks it gives identical output for seeds 0 and 1 on
the first 3 instances. Stochastic methods pass `GATE_SEEDS` (0 to 4).

A method is a registry name or a zero-argument callable returning a `Solver` (tests pass
fakes); `params` (optional on every call) become `profile.method_params[solver.name]`, the
solver's `SolveContext.params`. A `Bench` names the instances, the rules and the score
terms; `HELD_OUT_BENCH` is the D-083 set, and tests use a tiny bench with fake rules.
`MAKESPAN_BENCH` is the same instances scored by makespan alone (normalized by EDD's
makespan), for a makespan-only method whose D-083 bench-profile gate the orchestrator rules
structurally out of reach. Baseline scores are cached per bench for the session.
"""

from __future__ import annotations

import functools
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from bench.hmlv import HELD_OUT_SEEDS, hmlv_instance
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    SolveRequest,
    SolveResult,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import ParamValue, Solver
from twinflow.scheduling.solver import solve

Method = str | Callable[[], Solver]
Values = Mapping[ObjectiveKind, float]
Params = Mapping[str, ParamValue]
Terms = tuple[tuple[ObjectiveKind, float], ...]

RULES = ("fifo", "edd", "spt")
REFERENCE_RULE = "edd"
BENCH_TERMS: Terms = ((ObjectiveKind.WEIGHTED_TARDINESS, 1.0), (ObjectiveKind.TOTAL_SETUP, 0.2))
MAKESPAN_TERMS: Terms = ((ObjectiveKind.MAKESPAN, 1.0),)
WIN_SHARE = 0.6
MAX_CV = 0.05
GATE_SEEDS = (0, 1, 2, 3, 4)
STABILITY_SEEDS = (0, 1)
STABILITY_INSTANCES = 3
DEFAULT_BUDGET_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Bench:
    """Instances (`instance(seed)` for each of `instance_seeds`), the comparison rules and
    the score terms (kind, weight), each normalized by the reference rule's value."""

    instance: Callable[[int], SolveRequest]
    instance_seeds: tuple[int, ...]
    rules: tuple[tuple[str, Method], ...] = tuple((rule, rule) for rule in RULES)
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    terms: Terms = BENCH_TERMS

    @property
    def required_wins(self) -> int:
        return math.ceil(WIN_SHARE * len(self.instance_seeds) - 1e-9)

    def rule(self, name: str) -> Method:
        return dict(self.rules)[name]


HELD_OUT_BENCH = Bench(instance=hmlv_instance, instance_seeds=HELD_OUT_SEEDS)
MAKESPAN_BENCH = Bench(instance=hmlv_instance, instance_seeds=HELD_OUT_SEEDS, terms=MAKESPAN_TERMS)


@dataclass(frozen=True, slots=True)
class GateVerdict:
    """The D-083 verdict with the numbers behind it; `failures` says why it did not pass."""

    passed: bool
    seed_means: tuple[float, ...]
    wins: tuple[int, ...]
    required_wins: int
    rule_means: Mapping[str, float]
    cv: float
    failures: tuple[str, ...]


# --- profile and score ----------------------------------------------------------------------


def bench_profile(reference: Values | None, terms: Terms = BENCH_TERMS) -> WeightedSum:
    """The bench objective; each term is scaled by max(reference value, 1) (1 without one)."""
    return WeightedSum(
        terms=tuple(
            WeightedTerm(
                kind=kind,
                weight=weight,
                scale=max(reference[kind], 1.0) if reference is not None else 1.0,
            )
            for kind, weight in terms
        )
    )


def bench_score(spec: WeightedSum, values: Values) -> float:
    """sum weight x value / scale over the spec's terms."""
    return math.fsum(term.weight * values[term.kind] / (term.scale or 1.0) for term in spec.terms)


@functools.cache
def _instance(bench: Bench, instance_seed: int) -> SolveRequest:
    return bench.instance(instance_seed)


def _solver(method: Method) -> Solver:
    return get_solver(method) if isinstance(method, str) else method()


def _run(
    bench: Bench,
    method: Method,
    instance_seed: int,
    seed: int,
    objective: ObjectiveSpec,
    params: Params | None = None,
) -> SolveResult:
    request = _instance(bench, instance_seed)
    solver = _solver(method)
    update: dict[str, object] = {"objective": objective}
    if params is not None:
        update["method_params"] = {solver.name: dict(params)}
    profile = request.profile.model_copy(update=update)
    return solve(
        request.model_copy(update={"profile": profile}),
        solver=solver,
        budget_seconds=bench.budget_seconds,
        seed=seed,
    )


@functools.cache
def reference_values(bench: Bench, instance_seed: int) -> Values:
    """The reference rule's objective values on one instance; raises when it does not verify."""
    objective = bench_profile(None, bench.terms)
    result = _run(bench, bench.rule(REFERENCE_RULE), instance_seed, 0, objective)
    if not result.verified:
        raise RuntimeError(
            f"reference rule {REFERENCE_RULE} did not verify on instance {instance_seed}: "
            f"{[issue.code for issue in result.issues]}"
        )
    return result.objective_values


def _spec(bench: Bench, instance_seed: int) -> WeightedSum:
    return bench_profile(reference_values(bench, instance_seed), bench.terms)


def score(
    method: Method,
    instance_seed: int,
    seed: int = 0,
    *,
    bench: Bench = HELD_OUT_BENCH,
    params: Params | None = None,
) -> float:
    """The method's bench score on one instance; infinity when its answer does not verify."""
    spec = _spec(bench, instance_seed)
    result = _run(bench, method, instance_seed, seed, spec, params)
    return bench_score(spec, result.objective_values) if result.verified else math.inf


def _scores(
    method: Method, seed: int, bench: Bench, params: Params | None = None
) -> tuple[float, ...]:
    return tuple(score(method, i, seed, bench=bench, params=params) for i in bench.instance_seeds)


@functools.cache
def baseline_table(bench: Bench = HELD_OUT_BENCH) -> Mapping[str, tuple[float, ...]]:
    """Each rule's score per instance (in `instance_seeds` order), once per session."""
    return {name: _scores(method, 0, bench) for name, method in bench.rules}


def format_baseline_table(bench: Bench = HELD_OUT_BENCH) -> str:
    """One row per instance and a final mean row, one column per rule."""
    table = baseline_table(bench)
    names = list(table)
    lines = ["instance " + " ".join(f"{name:>8}" for name in names)]
    for row, seed in enumerate(bench.instance_seeds):
        lines.append(f"{seed:>8} " + " ".join(f"{table[n][row]:>8.4f}" for n in names))
    lines.append("mean     " + " ".join(f"{statistics.fmean(table[n]):>8.4f}" for n in names))
    return "\n".join(lines)


# --- the verdict ----------------------------------------------------------------------------


def _mean(scores: Sequence[float]) -> float:
    return math.inf if any(math.isinf(s) for s in scores) else statistics.fmean(scores)


def _cv(means: Sequence[float]) -> float:
    """pstdev / mean of the per-seed means; 0 for one seed or all zero, inf with an inf."""
    if any(math.isinf(m) for m in means):
        return math.inf
    center = statistics.fmean(means)
    spread = statistics.pstdev(means)
    if spread == 0.0:
        return 0.0
    return spread / abs(center) if center != 0.0 else math.inf


def judge(
    seed_scores: Sequence[Sequence[float]],
    table: Mapping[str, Sequence[float]],
    required_wins: int,
) -> GateVerdict:
    """Apply D-083 to per-seed instance scores against the rules' instance scores."""
    rule_means = {name: _mean(scores) for name, scores in table.items()}
    best = [min(column) for column in zip(*table.values(), strict=True)]
    failures: list[str] = []
    means, wins = [], []
    for index, scores in enumerate(seed_scores):
        mean = _mean(scores)
        won = sum(s < b for s, b in zip(scores, best, strict=True))
        means.append(mean)
        wins.append(won)
        failures += [
            f"seed #{index}: mean {mean} not below {name} {rule_mean}"
            for name, rule_mean in rule_means.items()
            if not mean < rule_mean
        ]
        if won < required_wins:
            failures.append(f"seed #{index}: {won} wins < {required_wins} required")
    cv = _cv(means)
    if not cv <= MAX_CV:
        failures.append(f"coefficient of variation {cv} above {MAX_CV}")
    return GateVerdict(
        passed=not failures,
        seed_means=tuple(means),
        wins=tuple(wins),
        required_wins=required_wins,
        rule_means=rule_means,
        cv=cv,
        failures=tuple(failures),
    )


def beats_baselines(
    method: Method,
    seeds: Sequence[int] = (0,),
    *,
    bench: Bench = HELD_OUT_BENCH,
    params: Params | None = None,
) -> GateVerdict:
    """D-083: does `method` beat FIFO, EDD and SPT on `bench` for every seed in `seeds`?"""
    seed_scores = [_scores(method, seed, bench, params) for seed in seeds]
    return judge(seed_scores, baseline_table(bench), bench.required_wins)


def seed_stable(
    method: Method, *, bench: Bench = HELD_OUT_BENCH, params: Params | None = None
) -> bool:
    """Seeds 0 and 1 give the same status and assignments on the first 3 instances."""
    for instance_seed in bench.instance_seeds[:STABILITY_INSTANCES]:
        spec = _spec(bench, instance_seed)
        outputs = {
            _run(bench, method, instance_seed, seed, spec, params).schedule.model_dump_json(
                include={"status", "assignments"}
            )
            for seed in STABILITY_SEEDS
        }
        if len(outputs) > 1:
            return False
    return True


def format_verdict(label: str, verdict: GateVerdict, instances: int) -> str:
    """One table row: label, per-seed means, wins per seed out of `instances`, CV, verdict."""
    means = " ".join(f"{m:.4f}" for m in verdict.seed_means)
    wins = " ".join(f"{w}/{instances}" for w in verdict.wins)
    outcome = "PASS" if verdict.passed else "FAIL"
    return f"{label:<28} mean {means:<8} wins {wins:<6} cv {verdict.cv:.4f} {outcome}"
