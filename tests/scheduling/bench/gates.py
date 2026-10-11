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
structurally out of reach. `FLOW_LINE_BENCH` is the held-out seeds on flow-line instances
(five_phase, DEV-19). Baseline scores are cached per bench for the session.

Session cache (T-166): every (bench, method, instance, seed, params) run happens once per
session; the gate verdict, the seed-stability check and the printed table reuse it.
`Portfolio(pipelines)` is a method too: `solve_portfolio` with the bench objective, the
answer is the recommended candidate (infinity when none verified). D-083 seed policy:
`gate_seeds` gives seed 0 for a method registered `deterministic=True` (a portfolio: every
stage deterministic and one worker) and `GATE_SEEDS` otherwise; `gate_report(GateCase)`
applies it (deterministic: the identical-output check for seeds 0 and 1 on 3 instances;
stochastic: the CV <= 0.05 of the verdict) and `format_gate_table` prints one table.
"""

from __future__ import annotations

import functools
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from bench.hmlv import HELD_OUT_SEEDS, flow_line_instance, hmlv_instance
from twinflow.scheduling import registry
from twinflow.scheduling.model import (
    ObjectiveKind,
    ObjectiveSpec,
    Schedule,
    SolveRequest,
    SolveResult,
    WeightedSum,
    WeightedTerm,
)
from twinflow.scheduling.portfolio.runner import STAGE_SEPARATOR, solve_portfolio
from twinflow.scheduling.seam import ParamValue, Solver
from twinflow.scheduling.solver import solve


@dataclass(frozen=True, slots=True)
class Portfolio:
    """A portfolio as a bench method: `solve_portfolio` over `pipelines` with `workers`."""

    pipelines: tuple[str, ...]
    workers: int = 1
    label: str = "portfolio"


BENCH_PORTFOLIO = Portfolio(("atcs", "atcs->tabu", "sb_wt", "edd"))
"""The default bench portfolio (T-166): deterministic pipelines on one worker, so its answer
is the same for every seed; beam and sa stay out (beam answers by its deadline, sa by seed)."""

Method = str | Callable[[], Solver] | Portfolio
Values = Mapping[ObjectiveKind, float]
Params = Mapping[str, ParamValue]
ParamsKey = tuple[tuple[str, ParamValue], ...] | None
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
FLOW_LINE_BENCH = Bench(instance=flow_line_instance, instance_seeds=HELD_OUT_SEEDS)


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


def _solver(method: str | Callable[[], Solver]) -> Solver:
    return registry.get(method) if isinstance(method, str) else method()


def _run(
    bench: Bench,
    method: Method,
    instance_seed: int,
    seed: int,
    objective: ObjectiveSpec,
    params: Params | None = None,
) -> SolveResult:
    request = _instance(bench, instance_seed)
    if isinstance(method, Portfolio):
        return _run_portfolio(bench, method, request, seed, objective)
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


def _run_portfolio(
    bench: Bench, portfolio: Portfolio, request: SolveRequest, seed: int, objective: ObjectiveSpec
) -> SolveResult:
    """The recommended candidate as a `SolveResult`; UNKNOWN with the issues when none."""
    profile = request.profile.model_copy(
        update={"objective": objective, "workers": portfolio.workers}
    )
    result = solve_portfolio(
        request.model_copy(update={"profile": profile}),
        budget_seconds=bench.budget_seconds,
        seed=seed,
        pipelines=portfolio.pipelines,
    )
    for candidate in result.candidates:
        if candidate.id == result.recommended:
            return SolveResult(
                schedule=candidate.schedule,
                objective_values=candidate.objective_values,
                verified=candidate.verified,
                issues=candidate.issues,
            )
    schedule = Schedule(
        status="UNKNOWN", assignments=(), solver=portfolio.label, elapsed_seconds=0.0
    )
    return SolveResult(schedule=schedule, verified=False, issues=result.issues)


def _params_key(params: Params | None) -> ParamsKey:
    return None if params is None else tuple(sorted(params.items()))


@functools.cache
def _cached_run(
    bench: Bench, method: Method, instance_seed: int, seed: int, params_key: ParamsKey
) -> SolveResult:
    """One run under the bench objective, once per session (module docstring)."""
    params = None if params_key is None else dict(params_key)
    return _run(bench, method, instance_seed, seed, _spec(bench, instance_seed), params)


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
    result = _cached_run(bench, method, instance_seed, seed, _params_key(params))
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
    key = _params_key(params)
    for instance_seed in bench.instance_seeds[:STABILITY_INSTANCES]:
        outputs = {
            _cached_run(bench, method, instance_seed, seed, key).schedule.model_dump_json(
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


# --- D-083 seed policy, gate reports and the table (T-166) -----------------------------------


def _stages(portfolio: Portfolio) -> set[str]:
    return {stage for pipeline in portfolio.pipelines for stage in pipeline.split(STAGE_SEPARATOR)}


def is_deterministic(method: Method) -> bool:
    """The registry flag; a portfolio: every stage deterministic and one worker."""
    flags = {info.name: info.deterministic for info in registry.available()}
    if isinstance(method, Portfolio):
        return method.workers == 1 and all(flags.get(stage, True) for stage in _stages(method))
    if isinstance(method, str):
        return flags[method]
    raise TypeError("a callable method has no registry flag; give GateCase.deterministic")


def gate_seeds(method: Method) -> tuple[int, ...]:
    """D-083: seed 0 for a deterministic method, `GATE_SEEDS` (0 to 4) otherwise."""
    return (0,) if is_deterministic(method) else GATE_SEEDS


def method_family(method: Method) -> str:
    """The registry family; "portfolio" for a portfolio, "fake" for a callable."""
    if isinstance(method, Portfolio):
        return "portfolio"
    if isinstance(method, str):
        return {info.name: info.family for info in registry.available()}[method]
    return "fake"


@dataclass(frozen=True, slots=True)
class GateCase:
    """One gate row: a method, its params (pairs, hashable), its bench and, for a callable
    method, whether it counts as deterministic (registry methods read the registry)."""

    label: str
    method: Method
    params: tuple[tuple[str, ParamValue], ...] = ()
    bench: Bench = HELD_OUT_BENCH
    deterministic: bool | None = None

    @property
    def method_params(self) -> Params | None:
        return dict(self.params) if self.params else None

    @property
    def is_deterministic(self) -> bool:
        if self.deterministic is not None:
            return self.deterministic
        return is_deterministic(self.method)


@dataclass(frozen=True, slots=True)
class GateReport:
    """A gate row's verdict; `stable` is the seeds 0/1 identity check (None: stochastic)."""

    case: GateCase
    family: str
    verdict: GateVerdict
    stable: bool | None

    @property
    def passed(self) -> bool:
        return self.verdict.passed and self.stable is not False


@functools.cache
def gate_report(case: GateCase) -> GateReport:
    """D-083 for one case, once per session (module docstring)."""
    seeds = (0,) if case.is_deterministic else GATE_SEEDS
    params = case.method_params
    verdict = beats_baselines(case.method, seeds, bench=case.bench, params=params)
    stable = seed_stable(case.method, bench=case.bench, params=params) if len(seeds) == 1 else None
    return GateReport(case, method_family(case.method), verdict, stable)


@dataclass(frozen=True, slots=True)
class ReferenceRow:
    """An elementary rule listed for reference (D-083 exempts it): mean score and wins."""

    label: str
    mean: float
    wins: int
    instances: int


def reference_row(label: str, method: Method, *, bench: Bench = HELD_OUT_BENCH) -> ReferenceRow:
    """The rule's seed-0 mean and wins against the best of FIFO, EDD and SPT on `bench`."""
    verdict = judge([_scores(method, 0, bench)], baseline_table(bench), bench.required_wins)
    return ReferenceRow(label, verdict.seed_means[0], verdict.wins[0], len(bench.instance_seeds))


_ROW = "{:<30} {:<14} {:>8} {:>13} {:>7} {:>7} {:>9}"


def _seed_check(report: GateReport) -> str:
    if report.stable is None:
        return "cv"
    return "same" if report.stable else "DIFFERS"


def format_gate_table(reports: Sequence[GateReport], references: Sequence[ReferenceRow]) -> str:
    """One row per gated method, then the reference rules (not gated)."""
    lines = [_ROW.format("method", "family", "mean", "wins", "cv", "seeds", "verdict")]
    for report in reports:
        verdict = report.verdict
        instances = len(report.case.bench.instance_seeds)
        lines.append(
            _ROW.format(
                report.case.label,
                report.family,
                f"{_mean(verdict.seed_means):.4f}",
                f"{min(verdict.wins)}/{instances}",
                f"{verdict.cv:.4f}",
                _seed_check(report),
                "PASS" if report.passed else "FAIL",
            )
        )
    for row in references:
        lines.append(
            _ROW.format(
                row.label,
                "dispatch",
                f"{row.mean:.4f}",
                f"{row.wins}/{row.instances}",
                "-",
                "-",
                "reference",
            )
        )
    return "\n".join(lines)
