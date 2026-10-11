"""Evaluator: replications under a budget, statistics, paired deltas, ranking (COMP-138).

`evaluate_candidates` runs every candidate plan, and optionally a dispatch baseline on the
first candidate's problem, for a number of replications, then reports per-metric
statistics, candidate-minus-baseline paired deltas and one ranking (REQ-134, REQ-136,
REQ-138).

- Deterministic analyses (planned objective values, `robustness_summary` with its load
  check and delta curve, the planned bottleneck) run once per candidate before any
  replication. They take no budget check of their own: their wall time counts against the
  budget because the clock starts before them.
- Deterministic collapse (D-096): when the source draws nothing for any shop, one
  replication runs and the result is flagged `deterministic`.
- Replication-major tasks under a budget (D-099): rep 0 of every candidate in input order,
  then the baseline, then rep 1, and so on. `clock` is read once at the start and once
  before each submission; a task goes out only while `clock() - start < budget_seconds`.
  Only replications every candidate and the baseline finished are kept. A pool also reads
  it while waiting for a result and stops waiting when the budget is spent, so a stalled
  or dead worker cannot hold the call (its workers are terminated).
- Workers (D-099, D-028): with `workers == 1` tasks run in process; otherwise a spawn pool
  whose initializer receives the shop models, controls and source once per worker, and at
  most `workers` tasks are in flight so the budget check stays meaningful. Replication r
  always uses `RepDraws(seed, r, source)` and results are keyed by (run, rep), so the
  worker count never changes a result.
- Statistics: Student-t interval on the mean at `policy.confidence_level`; paired deltas
  by the `DifferenceCI` method of `twinflow/plan/replication.py` (D-103), reimplemented
  here because the execution package does not import `twinflow.plan`.
- Ranking: `objectives.rank` over mean objective values under the first candidate's
  profile objective (D-068, the single ranking path).

Pure: no file or network I/O.
"""

from __future__ import annotations

import math
import multiprocessing
import statistics
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from multiprocessing.pool import AsyncResult
from typing import Any, Literal

import numpy as np
import scipy.stats  # type: ignore[import-untyped]  # no stub package; SciPy is the D-036-endorsed library

from twinflow.execution import engine, metrics
from twinflow.execution.engine import DispatchControl, ExecutionRun, Limits, PlanControl, RepDraws
from twinflow.execution.metrics import METRIC_NAMES, ReplicationMetrics
from twinflow.execution.policy import SimulationPolicy
from twinflow.execution.robustness import RobustnessSummary, robustness_summary
from twinflow.execution.shop import ShopModel, build_shop
from twinflow.execution.sources import DistributionSource
from twinflow.policy import DISPATCH_NAMES
from twinflow.scheduling import objectives
from twinflow.scheduling.model import CompiledProblem, ObjectiveKind, Schedule

Status = Literal["completed", "truncated"]
Task = tuple[int, int]
"""(run index, replication): run i < len(candidates) is candidate i, the last the baseline."""
Outcome = tuple[ExecutionRun, ReplicationMetrics]

BASELINE_ROW_ID = "baseline"
"""The `plan_id` written into the baseline's evidence rows (it has no plan)."""


# --- public types ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateInput:
    """One plan to evaluate: its id, the compiled problem it was verified on, the plan."""

    plan_id: str
    compiled: CompiledProblem
    schedule: Schedule


@dataclass(frozen=True, slots=True)
class Stats:
    """Distribution of one metric over replications; `ci_*` None when n < 2."""

    mean: float
    std: float
    min: float
    p10: float
    p50: float
    p90: float
    max: float
    ci_low: float | None
    ci_high: float | None
    n: int


@dataclass(frozen=True, slots=True)
class PairedDelta:
    """Candidate minus reference over common replications (D-103); `low`/`high` None when
    n < 2."""

    mean_difference: float
    low: float | None
    high: float | None
    n: int


@dataclass(frozen=True, slots=True)
class Bottleneck:
    """The planned top machine by busy share and that share."""

    machine_id: str
    planned_utilization: float


@dataclass(frozen=True)
class CandidateEvaluation:
    """Everything evaluated for one candidate, or for the baseline (`plan_id` None, no
    planned values, robustness or bottleneck). Replication tuples are in rep order."""

    plan_id: str | None
    planned: Mapping[ObjectiveKind, float]
    per_rep: tuple[ReplicationMetrics, ...]
    runs: tuple[ExecutionRun, ...]
    metrics: Mapping[str, Stats]
    robustness: RobustnessSummary | None
    bottleneck: Bottleneck | None
    operation_rows: tuple[dict[str, Any], ...]
    job_rows: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class EvaluationResult:
    """The COMP-138 output. `replications` is the kept count k; `candidates` keep input
    order; `paired[plan_id][metric]` is empty without a baseline."""

    status: Status
    replications: int
    deterministic: bool
    candidates: tuple[CandidateEvaluation, ...]
    baseline: CandidateEvaluation | None
    paired: Mapping[str, Mapping[str, PairedDelta]]
    ranking: tuple[str, ...]
    elapsed_seconds: float


class BudgetExhausted(RuntimeError):
    """The budget ran out before every candidate (and the baseline) finished replication 0."""

    code = "budget_exhausted"


# --- statistics --------------------------------------------------------------------------------


def _t_critical(confidence: float, n: int) -> float:
    """Two-sided Student-t critical value with n - 1 degrees of freedom."""
    return float(scipy.stats.t.ppf(1 - (1 - confidence) / 2, df=n - 1))


def stats(values: Sequence[float], confidence: float) -> Stats:
    """Mean, sample std (ddof 1; 0.0 for one value), min, linear p10/p50/p90, max and the
    Student-t interval on the mean (None when fewer than two values)."""
    if not values:
        raise ValueError("stats needs at least one value")
    data = np.asarray(values, dtype=float)
    n = len(data)
    mean = float(data.mean())
    std = float(np.std(data, ddof=1)) if n >= 2 else 0.0
    p10, p50, p90 = (float(np.percentile(data, q, method="linear")) for q in (10, 50, 90))
    low: float | None = None
    high: float | None = None
    if n >= 2:
        half_width = _t_critical(confidence, n) * (std / math.sqrt(n))
        low, high = mean - half_width, mean + half_width
    return Stats(
        mean=mean,
        std=std,
        min=float(data.min()),
        p10=p10,
        p50=p50,
        p90=p90,
        max=float(data.max()),
        ci_low=low,
        ci_high=high,
        n=n,
    )


def paired_delta(
    candidate: Mapping[int, float], reference: Mapping[int, float], confidence: float
) -> PairedDelta:
    """Candidate minus reference over the common replication indices (D-103).

    The `DifferenceCI` method of `plan/replication.py`: mean difference +/- t x stderr,
    stderr 0 when the population std of the differences is 0.
    """
    common = sorted(candidate.keys() & reference.keys())
    if not common:
        raise ValueError("paired_delta needs at least one common replication")
    diffs = [candidate[r] - reference[r] for r in common]
    n = len(diffs)
    mean = statistics.fmean(diffs)
    if n < 2:
        return PairedDelta(mean_difference=mean, low=None, high=None, n=n)
    stderr = 0.0 if statistics.pstdev(diffs) == 0.0 else statistics.stdev(diffs) / math.sqrt(n)
    half_width = _t_critical(confidence, n) * stderr
    return PairedDelta(mean_difference=mean, low=mean - half_width, high=mean + half_width, n=n)


# --- the work every replication needs (sent once per worker, D-028) ----------------------------


@dataclass(frozen=True)
class _Run:
    """One simulated line: a candidate's plan control, or the baseline's dispatch control."""

    shop_index: int
    control: PlanControl | DispatchControl
    limits: Limits
    plan: Schedule | None


@dataclass(frozen=True)
class _Work:
    """Shops (one per distinct compiled problem), their problems, the runs and the draws."""

    compiled: tuple[CompiledProblem, ...]
    shops: tuple[ShopModel, ...]
    runs: tuple[_Run, ...]
    source: DistributionSource
    seed: int


def _replicate(work: _Work, run_index: int, replication: int) -> Outcome:
    """Execute one (run, replication) task and compute its replication metrics."""
    spec = work.runs[run_index]
    shop = work.shops[spec.shop_index]
    draws = RepDraws(seed=work.seed, replication=replication, source=work.source)
    run = engine.execute(shop, spec.control, draws, (), spec.limits)
    values = metrics.replication_metrics(work.compiled[spec.shop_index], shop, run, spec.plan)
    return run, values


_WORKER_STATE: dict[str, _Work] = {}
"""Per-process work of a spawned pool worker, written only by `_init_worker` (D-028)."""


def _init_worker(work: _Work) -> None:
    """Pool initializer: keep the work for every task this worker runs."""
    _WORKER_STATE["work"] = work


def _replicate_in_worker(run_index: int, replication: int) -> Outcome:
    """A pool task: one replication against the work the initializer stored."""
    return _replicate(_WORKER_STATE["work"], run_index, replication)


def _run_in_process(
    work: _Work, tasks: Sequence[Task], may_submit: Callable[[], bool]
) -> dict[Task, Outcome]:
    """Run tasks in order while the budget allows."""
    outcomes: dict[Task, Outcome] = {}
    for task in tasks:
        if not may_submit():
            break
        outcomes[task] = _replicate(work, *task)
    return outcomes


def _run_in_pool(
    work: _Work, tasks: Sequence[Task], workers: int, time_left: Callable[[], float]
) -> dict[Task, Outcome]:
    """Run tasks in a spawn pool, at most `workers` in flight, oldest collected first.

    Waiting for a result also stops at the budget: a stalled or dead worker (the pool never
    completes a task whose worker died) drops the work still in flight, and leaving the
    `with` block terminates and joins every worker.
    """
    outcomes: dict[Task, Outcome] = {}
    context = multiprocessing.get_context("spawn")
    processes = min(workers, len(tasks))
    with context.Pool(processes, initializer=_init_worker, initargs=(work,)) as pool:
        in_flight: deque[tuple[Task, AsyncResult[Outcome]]] = deque()
        try:
            for task in tasks:
                if len(in_flight) >= processes:
                    _collect(in_flight.popleft(), outcomes, time_left)
                if time_left() <= 0.0:
                    break
                in_flight.append((task, pool.apply_async(_replicate_in_worker, task)))
            while in_flight:
                _collect(in_flight.popleft(), outcomes, time_left)
        except multiprocessing.TimeoutError:
            pass  # budget spent with work in flight; only finished replications are kept
    return outcomes


def _collect(
    entry: tuple[Task, AsyncResult[Outcome]],
    outcomes: dict[Task, Outcome],
    time_left: Callable[[], float],
) -> None:
    """Store one in-flight result, waiting no longer than the budget left."""
    task, result = entry
    outcomes[task] = result.get(timeout=max(time_left(), 0.0))


def _run_tasks(
    work: _Work, tasks: Sequence[Task], workers: int, time_left: Callable[[], float]
) -> dict[Task, Outcome]:
    """In process for one worker, else a spawn pool (D-099)."""
    if workers == 1 or len(tasks) <= 1:
        return _run_in_process(work, tasks, lambda: time_left() > 0.0)
    return _run_in_pool(work, tasks, workers, time_left)


def _complete_replications(outcomes: Mapping[Task, Outcome], runs: int, replications: int) -> int:
    """k: the leading replications every run finished."""
    for replication in range(replications):
        if any((run, replication) not in outcomes for run in range(runs)):
            return replication
    return replications


# --- preparation and per-candidate analyses -----------------------------------------------------


def _check_inputs(
    candidates: Sequence[CandidateInput], replications: int, workers: int, baseline: str | None
) -> None:
    """Raise `ValueError` for any refused input (see `evaluate_candidates`)."""
    if not candidates:
        raise ValueError("at least one candidate is required")
    ids = [c.plan_id for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate plan ids must be unique")
    if replications < 1:
        raise ValueError("replications must be at least 1")
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if baseline is not None and baseline not in DISPATCH_NAMES:
        raise ValueError(f"unknown baseline rule {baseline!r}; expected one of {DISPATCH_NAMES}")


def _shops(candidates: Sequence[CandidateInput]) -> tuple[list[CompiledProblem], list[int]]:
    """Distinct compiled problems (by identity) and each candidate's index into them."""
    problems: list[CompiledProblem] = []
    index_of: dict[int, int] = {}
    indexes: list[int] = []
    for candidate in candidates:
        key = id(candidate.compiled)
        if key not in index_of:
            index_of[key] = len(problems)
            problems.append(candidate.compiled)
        indexes.append(index_of[key])
    return problems, indexes


def _planned_makespan(shop: ShopModel, plan: Schedule) -> float:
    """Planned last end minus now: the `Limits.for_shop` input (D-107)."""
    return metrics.plan_end(shop, plan) - shop.now


def _prepare_work(
    candidates: Sequence[CandidateInput],
    baseline: str | None,
    source: DistributionSource,
    seed: int,
) -> _Work:
    """One shop per distinct problem; a plan run per candidate, then the baseline run.

    The baseline runs on the first candidate's shop with that plan's T_c (same problem).
    """
    problems, indexes = _shops(candidates)
    shops = tuple(build_shop(compiled) for compiled in problems)
    runs = [
        _Run(
            shop_index=index,
            control=PlanControl(c.schedule),
            limits=Limits.for_shop(shops[index], _planned_makespan(shops[index], c.schedule)),
            plan=c.schedule,
        )
        for c, index in zip(candidates, indexes, strict=True)
    ]
    if baseline is not None:
        first = runs[0]
        runs.append(_Run(first.shop_index, DispatchControl(baseline), first.limits, None))
    return _Work(tuple(problems), shops, tuple(runs), source, seed)


@dataclass(frozen=True)
class _Analysis:
    """The deterministic analyses of one candidate."""

    planned: Mapping[ObjectiveKind, float]
    robustness: RobustnessSummary
    bottleneck: Bottleneck | None


def _bottleneck(shop: ShopModel, plan: Schedule) -> Bottleneck | None:
    """The planned top machine by busy share over [now, planned end]; None without one."""
    shares = metrics.busy_shares(shop, metrics.plan_occupation(plan), metrics.plan_end(shop, plan))
    machine_id = metrics.top_machine(shares)
    return None if machine_id is None else Bottleneck(machine_id, shares[machine_id])


def _analyse(
    candidate: CandidateInput,
    shop: ShopModel,
    policy: SimulationPolicy,
    targets: Sequence[float],
) -> _Analysis:
    """Planned values, robustness summary and planned bottleneck of one candidate."""
    compiled, plan = candidate.compiled, candidate.schedule
    return _Analysis(
        planned=objectives.evaluate(compiled, plan, None),
        robustness=robustness_summary(shop, compiled, plan, policy, targets),
        bottleneck=_bottleneck(shop, plan),
    )


# --- assembling the result ----------------------------------------------------------------------


def _evaluation(
    plan_id: str | None,
    analysis: _Analysis | None,
    outcomes: Sequence[Outcome],
    shop: ShopModel,
    plan: Schedule | None,
    confidence: float,
) -> CandidateEvaluation:
    """One evaluation from its kept outcomes (rep order) and its analysis, if any."""
    runs = tuple(run for run, _ in outcomes)
    per_rep = tuple(values for _, values in outcomes)
    rows_id = BASELINE_ROW_ID if plan_id is None else plan_id
    return CandidateEvaluation(
        plan_id=plan_id,
        planned={} if analysis is None else analysis.planned,
        per_rep=per_rep,
        runs=runs,
        metrics=_metric_stats(per_rep, confidence),
        robustness=None if analysis is None else analysis.robustness,
        bottleneck=None if analysis is None else analysis.bottleneck,
        operation_rows=metrics.operation_rows(rows_id, runs, plan),
        job_rows=metrics.job_rows(rows_id, shop, runs, plan),
    )


def _metric_stats(per_rep: Sequence[ReplicationMetrics], confidence: float) -> dict[str, Stats]:
    """`stats` of every `METRIC_NAMES` metric over the replications."""
    values = [rep.metric_values() for rep in per_rep]
    return {name: stats([v[name] for v in values], confidence) for name in METRIC_NAMES}


def _paired(
    candidates: Sequence[CandidateEvaluation], baseline: CandidateEvaluation, confidence: float
) -> dict[str, dict[str, PairedDelta]]:
    """Every candidate minus the baseline, per metric, over common replications."""
    reference = [rep.metric_values() for rep in baseline.per_rep]
    paired: dict[str, dict[str, PairedDelta]] = {}
    for evaluation in candidates:
        values = [rep.metric_values() for rep in evaluation.per_rep]
        paired[str(evaluation.plan_id)] = {
            name: paired_delta(
                {r: v[name] for r, v in enumerate(values)},
                {r: v[name] for r, v in enumerate(reference)},
                confidence,
            )
            for name in METRIC_NAMES
        }
    return paired


def _ranking(candidates: Sequence[CandidateEvaluation], compiled: CompiledProblem) -> list[str]:
    """`objectives.rank` over each candidate's mean objective values (D-068)."""
    means = {
        str(c.plan_id): {
            kind: float(np.mean([rep.objective_values[kind] for rep in c.per_rep]))
            for kind in ObjectiveKind
        }
        for c in candidates
    }
    return objectives.rank(means, compiled.profile.objective)


def _kept_evaluation(
    work: _Work,
    outcomes: Mapping[Task, Outcome],
    kept: int,
    run: int,
    plan_id: str | None,
    analysis: _Analysis | None,
    confidence: float,
) -> CandidateEvaluation:
    """The evaluation of run `run` over replications 0..kept-1."""
    spec = work.runs[run]
    kept_outcomes = [outcomes[(run, r)] for r in range(kept)]
    shop = work.shops[spec.shop_index]
    return _evaluation(plan_id, analysis, kept_outcomes, shop, spec.plan, confidence)


def evaluate_candidates(
    candidates: Sequence[CandidateInput],
    *,
    source: DistributionSource,
    policy: SimulationPolicy,
    targets: Sequence[float],
    seed: int,
    replications: int,
    budget_seconds: float,
    workers: int,
    baseline_rule: str | None,
    clock: Callable[[], float] = time.monotonic,
) -> EvaluationResult:
    """Evaluate `candidates` (and a dispatch baseline when `baseline_rule` is set).

    Raises `ValueError` for no candidates, duplicate plan ids, `replications < 1`,
    `workers < 1` or an unknown baseline rule, and `BudgetExhausted` when the budget ends
    before replication 0 finished everywhere. See the module docstring for the budget,
    worker and statistics contracts.
    """
    _check_inputs(candidates, replications, workers, baseline_rule)
    start = clock()
    work = _prepare_work(candidates, baseline_rule, source, seed)
    analyses = [
        _analyse(c, work.shops[work.runs[i].shop_index], policy, targets)
        for i, c in enumerate(candidates)
    ]
    deterministic = not any(source.is_stochastic(shop) for shop in work.shops)
    planned_reps = 1 if deterministic else replications
    tasks = [(run, r) for r in range(planned_reps) for run in range(len(work.runs))]
    outcomes = _run_tasks(work, tasks, workers, lambda: budget_seconds - (clock() - start))
    kept = _complete_replications(outcomes, len(work.runs), planned_reps)
    if kept == 0:
        raise BudgetExhausted("budget_exhausted: no replication finished for every plan")

    confidence = policy.confidence_level
    evaluations = tuple(
        _kept_evaluation(work, outcomes, kept, i, c.plan_id, analyses[i], confidence)
        for i, c in enumerate(candidates)
    )
    baseline = None
    if baseline_rule is not None:
        baseline = _kept_evaluation(work, outcomes, kept, len(candidates), None, None, confidence)
    return EvaluationResult(
        status="completed" if kept == planned_reps else "truncated",
        replications=kept,
        deterministic=deterministic,
        candidates=evaluations,
        baseline=baseline,
        paired={} if baseline is None else _paired(evaluations, baseline, confidence),
        ranking=tuple(_ranking(evaluations, candidates[0].compiled)),
        elapsed_seconds=clock() - start,
    )


__all__ = [
    "BASELINE_ROW_ID",
    "Bottleneck",
    "BudgetExhausted",
    "CandidateEvaluation",
    "CandidateInput",
    "EvaluationResult",
    "PairedDelta",
    "Stats",
    "evaluate_candidates",
    "paired_delta",
    "stats",
]
