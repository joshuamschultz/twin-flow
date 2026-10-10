"""Objective senses, evaluation and ranking of schedules (COMP-005, D-068).

`evaluate` computes all thirteen `ObjectiveKind` values of one schedule with the SDD
"Objective formulas". `rank` orders candidate schedules by their values under an
`ObjectiveSpec`. Both are pure, deterministic and linear in the number of operations
and candidates they read.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    EpsilonConstraint,
    Job,
    Lexicographic,
    LexicographicLevel,
    ObjectiveKind,
    ObjectiveSpec,
    PreviousPlan,
    Schedule,
    WeightedSum,
)

Sense = Literal["min", "max"]
ObjectiveValues = Mapping[ObjectiveKind, float]

SENSE: Mapping[ObjectiveKind, Sense] = MappingProxyType(
    {kind: "max" if kind is ObjectiveKind.ROBUSTNESS else "min" for kind in ObjectiveKind}
)
"""Optimization direction per objective: robustness R(S) is maximized, the rest minimized."""

SECONDS_PER_HOUR = 3600.0


def primary_kind(spec: ObjectiveSpec) -> ObjectiveKind:
    """The objective a single-objective solver optimizes for `spec` (D-068).

    Lexicographic: the first level. Weighted sum: the first (or only) term.
    Epsilon-constraint: its `primary`.
    """
    if isinstance(spec, Lexicographic):
        return spec.levels[0].kind
    if isinstance(spec, WeightedSum):
        return spec.terms[0].kind
    return spec.primary


def _signed(kind: ObjectiveKind, value: float) -> float:
    """`value` turned into a quantity to minimize: maximize-sense values are negated."""
    return -value if SENSE[kind] == "max" else value


# --- evaluation -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _JobOutcome:
    """A scheduled job: effective weight, completion C_j and due d_j (None when no due)."""

    weight: float
    completion: float
    due: float | None


@dataclass(frozen=True)
class _Index:
    """Dict lookups over the compiled problem so evaluation is O(operations)."""

    job_of_operation: Mapping[str, Job]
    option_cost: Mapping[tuple[str, str], float]
    hour_cost: Mapping[str, tuple[float, float]]


def evaluate(
    compiled: CompiledProblem, schedule: Schedule, previous_plan: PreviousPlan | None = None
) -> dict[ObjectiveKind, float]:
    """All thirteen objective values of `schedule`, per the SDD "Objective formulas" (D-068).

    Scope: assignments of operations of held jobs (`compiled.held_job_ids`) or of
    operations not in the problem are ignored. A job counts in job terms only when at least
    one of its operations is assigned; C_j is the latest `end` among them. Jobs without a
    due date are left out of every due-date term. Effective weight is `weight_override`
    (written by override compilation), else the profile's class weight. Times are measured
    from `problem.now`. Empty aggregates (no scheduled job, no due job) give 0.

    Churn counts operations present in both `previous_plan` and `schedule` whose machine
    changed or whose start moved by more than the frozen-zone tolerance (0 when the profile
    has no frozen zone); 0 without a previous plan.

    The schedule is expected to have passed `verify`: an assignment to a machine that is not
    in the problem or not an option of its operation raises `KeyError`.
    """
    index = _build_index(compiled)
    assignments = [a for a in schedule.assignments if a.operation_id in index.job_of_operation]
    outcomes = _job_outcomes(compiled, index, assignments)
    now = compiled.problem.now
    values = _completion_terms(outcomes, now) | _due_terms(outcomes, now)
    values |= _assignment_terms(assignments, index)
    values[ObjectiveKind.CHURN] = _churn(assignments, previous_plan, _churn_tolerance(compiled))
    return values


def _build_index(compiled: CompiledProblem) -> _Index:
    held = set(compiled.held_job_ids)
    job_of_operation: dict[str, Job] = {}
    option_cost: dict[tuple[str, str], float] = {}
    for job in compiled.problem.jobs:
        if job.id in held:
            continue
        for operation in job.operations:
            job_of_operation[operation.id] = job
            for option in operation.options:
                option_cost[operation.id, option.machine_id] = option.preference_cost
    hour_cost = {m.id: (m.hour_cost, m.setup_hour_cost) for m in compiled.problem.machines}
    return _Index(job_of_operation, option_cost, hour_cost)


def _job_outcomes(
    compiled: CompiledProblem, index: _Index, assignments: Iterable[Assignment]
) -> list[_JobOutcome]:
    jobs: dict[str, Job] = {}
    completion: dict[str, float] = {}
    for assignment in assignments:
        job = index.job_of_operation[assignment.operation_id]
        jobs[job.id] = job
        completion[job.id] = max(completion.get(job.id, -math.inf), assignment.end)
    class_weights = compiled.profile.class_weights
    return [
        _JobOutcome(
            weight=(
                job.weight_override
                if job.weight_override is not None
                else class_weights[job.weight_class]
            ),
            completion=completion[job_id],
            due=job.due,
        )
        for job_id, job in jobs.items()
    ]


def _completion_terms(outcomes: Sequence[_JobOutcome], now: float) -> dict[ObjectiveKind, float]:
    makespan = max((o.completion for o in outcomes), default=now) - now
    return {
        ObjectiveKind.MAKESPAN: makespan,
        ObjectiveKind.WEIGHTED_COMPLETION: math.fsum(o.weight * o.completion for o in outcomes),
    }


def _due_terms(outcomes: Sequence[_JobOutcome], now: float) -> dict[ObjectiveKind, float]:
    due_jobs = [(o.weight, o.completion, o.due) for o in outcomes if o.due is not None]
    lateness = [(w, c - d) for w, c, d in due_jobs]
    tardy = [(w, late) for w, late in lateness if late > 0]
    return {
        ObjectiveKind.MAX_LATENESS: max((late for _, late in lateness), default=0.0),
        ObjectiveKind.TARDY_COUNT: float(len(tardy)),
        ObjectiveKind.WEIGHTED_TARDY_COUNT: math.fsum(w for w, _ in tardy),
        ObjectiveKind.TOTAL_TARDINESS: math.fsum(late for _, late in tardy),
        ObjectiveKind.WEIGHTED_TARDINESS: math.fsum(w * late for w, late in tardy),
        ObjectiveKind.EARLINESS_TARDINESS: math.fsum(abs(late) for _, late in lateness),
        ObjectiveKind.ROBUSTNESS: _robustness(due_jobs, now),
    }


def _robustness(due_jobs: Sequence[tuple[float, float, float]], now: float) -> float:
    """R(S) = sum w_j (d_j - C_j) / sum w_j (d_j - now) (Pinedo p.483); 0 when undefined."""
    denominator = math.fsum(w * (d - now) for w, _, d in due_jobs)
    if denominator <= 0:
        return 0.0
    return math.fsum(w * (d - c) for w, c, d in due_jobs) / denominator


def _assignment_terms(
    assignments: Sequence[Assignment], index: _Index
) -> dict[ObjectiveKind, float]:
    soft_penalty = 0.0
    cost_seconds: list[float] = []
    for a in assignments:
        hour_cost, setup_hour_cost = index.hour_cost[a.machine_id]
        cost_seconds.append((a.end - a.setup_end) * hour_cost)
        cost_seconds.append((a.setup_end - a.start) * setup_hour_cost)
        soft_penalty += index.option_cost[a.operation_id, a.machine_id]
    return {
        ObjectiveKind.TOTAL_SETUP: math.fsum(a.setup_end - a.start for a in assignments),
        ObjectiveKind.COST: math.fsum(cost_seconds) / SECONDS_PER_HOUR,
        ObjectiveKind.SOFT_PENALTY: soft_penalty,
    }


def _churn_tolerance(compiled: CompiledProblem) -> float:
    frozen_zone = compiled.profile.frozen_zone
    return frozen_zone.tolerance if frozen_zone is not None else 0.0


def _churn(
    assignments: Iterable[Assignment], previous_plan: PreviousPlan | None, tolerance: float
) -> float:
    if previous_plan is None:
        return 0.0
    current = {a.operation_id: a for a in assignments}
    changed = 0
    for previous in previous_plan.assignments:
        now_at = current.get(previous.operation_id)
        if now_at is None:
            continue
        moved = abs(now_at.start - previous.start) > tolerance
        if moved or now_at.machine_id != previous.machine_id:
            changed += 1
    return float(changed)


# --- ranking ----------------------------------------------------------------------------------


def rank(
    candidates: Mapping[str, ObjectiveValues],
    spec: ObjectiveSpec,
    reference: str | None = None,
) -> list[str]:
    """Candidate ids ordered best first under `spec`; exact ties break by id (D-068).

    Weighted sum: score = sum weight * signed(value) / scale, where a term without `scale`
    is divided by |reference candidate's value| (1 when that value is 0 or no `reference`
    is given). Lexicographic and epsilon-constraint: see `_rank_lexicographic` and
    `_rank_epsilon`. The order depends only on the values and ids, never on mapping order.

    Raises `KeyError` when `reference` is not a candidate or a needed value is missing.
    """
    if isinstance(spec, WeightedSum):
        return _rank_weighted_sum(candidates, spec, reference)
    if isinstance(spec, Lexicographic):
        return _rank_lexicographic(sorted(candidates), candidates, spec.levels)
    return _rank_epsilon(candidates, spec)


def _rank_weighted_sum(
    candidates: Mapping[str, ObjectiveValues], spec: WeightedSum, reference: str | None
) -> list[str]:
    reference_values = candidates[reference] if reference is not None else None
    scales = [_term_scale(term.kind, term.scale, reference_values) for term in spec.terms]

    def score(values: ObjectiveValues) -> float:
        return math.fsum(
            term.weight * _signed(term.kind, values[term.kind]) / scale
            for term, scale in zip(spec.terms, scales, strict=True)
        )

    return sorted(candidates, key=lambda cid: (score(candidates[cid]), cid))


def _term_scale(
    kind: ObjectiveKind, scale: float | None, reference_values: ObjectiveValues | None
) -> float:
    if scale is not None:
        return scale
    if reference_values is None:
        return 1.0
    return abs(reference_values[kind]) or 1.0


def _rank_lexicographic(
    group: list[str],
    candidates: Mapping[str, ObjectiveValues],
    levels: Sequence[LexicographicLevel],
) -> list[str]:
    """Order `group` (sorted ids) by `levels`, with tolerance ties resolved by banding.

    Pairwise "within tolerance" is not transitive, so it is not used as a comparator.
    Instead, at each level: take the best signed value among the candidates still unplaced;
    the band is every unplaced candidate within tolerance of that best (absolute, or
    `tolerance * |best|` when relative); the band is ordered recursively by the next
    levels and placed; repeat on the remaining candidates. With no levels left, id order.
    """
    if not levels or len(group) <= 1:
        return group
    level, rest_levels = levels[0], levels[1:]
    value = {cid: _signed(level.kind, candidates[cid][level.kind]) for cid in group}
    ordered: list[str] = []
    remaining = group
    while remaining:
        best = min(value[cid] for cid in remaining)
        limit = best + _tolerance(level, best)
        band = [cid for cid in remaining if value[cid] <= limit]
        remaining = [cid for cid in remaining if value[cid] > limit]
        ordered.extend(_rank_lexicographic(band, candidates, rest_levels))
    return ordered


def _tolerance(level: LexicographicLevel, best: float) -> float:
    if level.tolerance_kind == "relative":
        return level.tolerance * abs(best)
    return level.tolerance


def _rank_epsilon(candidates: Mapping[str, ObjectiveValues], spec: EpsilonConstraint) -> list[str]:
    """Bound-satisfying candidates first by signed primary; violators after by total violation.

    Total violation is the sum over bounds of max(0, value - upper) on raw values.
    """

    def key(cid: str) -> tuple[bool, float, str]:
        values = candidates[cid]
        violation = math.fsum(max(0.0, values[b.kind] - b.upper) for b in spec.bounds)
        if violation > 0:
            return (True, violation, cid)
        return (False, _signed(spec.primary, values[spec.primary]), cid)

    return sorted(candidates, key=key)
