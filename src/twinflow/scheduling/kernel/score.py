"""Fast evaluator and scorer (COMP-006, REQ-111, REQ-089, SDD Data Model "Scorer").

`fast_values(index, decoded, previous_plan, churn_tolerance)` gives the thirteen objective
values of a decoded schedule straight from its per-position tuples, without building a
`Schedule`. It equals `objectives.evaluate` within 1e-9: the job-level terms go through
the same `objectives.job_terms`, and the assignment terms (total setup, cost, soft
penalty) and churn follow the same formulas over the decoded ops (the index holds exactly
the ops `evaluate` scores: not done, of open jobs; held jobs are absent from the compiled
problem).

`Scorer(spec, reference)` compares objective-value sets the way `objectives.rank` orders
them, using rank's own arithmetic (`signed`, `term_scale`, `level_tolerance`,
`bound_violation`). `reference` is the value set of the run's first verified schedule (or
None); it fixes weighted-sum scales and the lexicographic `delta` scale.

- `key(values)`: weighted sum -> (score,); lexicographic -> signed values by level (a total
  order only when every tolerance is 0); epsilon -> (0, signed primary) when every bound
  holds, else (1, total violation).
- `better(a, b)`: a strictly before b. Lexicographic compares level by level; two values
  tie at a level when both lie within the level's tolerance of the better one (rank's band
  for two candidates).
- `delta(a, b)`: negative when a is better, positive when b is, 0 on a tie. Weighted sum:
  the score difference. Lexicographic: the first deciding level's signed difference over
  |reference value| (1 when 0 or no reference). Epsilon: the violation difference, else
  the scaled primary difference.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TypeAlias

from twinflow.scheduling.kernel.decode import Decoded
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import (
    EpsilonConstraint,
    Lexicographic,
    ObjectiveKind,
    ObjectiveSpec,
    PreviousPlan,
    WeightedSum,
)
from twinflow.scheduling.objectives import (
    SECONDS_PER_HOUR,
    JobOutcome,
    bound_violation,
    job_terms,
    level_tolerance,
    signed,
    term_scale,
)

Values: TypeAlias = Mapping[ObjectiveKind, float]


# --- fast evaluation ---------------------------------------------------------------------------


def fast_values(
    index: ProblemIndex,
    decoded: Decoded,
    previous_plan: PreviousPlan | None,
    churn_tolerance: float,
) -> dict[ObjectiveKind, float]:
    """All thirteen objective values of `decoded` (equal to `objectives.evaluate`)."""
    ends = decoded.ends
    outcomes = [
        JobOutcome(weight=job.weight, completion=max(ends[p] for p in job.ops), due=job.due)
        for job in index.jobs
    ]
    values = job_terms(outcomes, index.now) | _assignment_terms(index, decoded)
    values[ObjectiveKind.CHURN] = _churn(index, decoded, previous_plan, churn_tolerance)
    return values


def _assignment_terms(index: ProblemIndex, decoded: Decoded) -> dict[ObjectiveKind, float]:
    hour_cost = {m.id: (m.hour_cost, m.setup_hour_cost) for m in index.machines}
    setups: list[float] = []
    cost_seconds: list[float] = []
    for p, option in enumerate(decoded.options):
        start, setup_end, end = decoded.starts[p], decoded.setup_ends[p], decoded.ends[p]
        run_rate, setup_rate = hour_cost[option.machine_id]
        setups.append(setup_end - start)
        cost_seconds.append((end - setup_end) * run_rate)
        cost_seconds.append((setup_end - start) * setup_rate)
    return {
        ObjectiveKind.TOTAL_SETUP: math.fsum(setups),
        ObjectiveKind.COST: math.fsum(cost_seconds) / SECONDS_PER_HOUR,
        ObjectiveKind.SOFT_PENALTY: math.fsum(o.preference_cost for o in decoded.options),
    }


def _churn(
    index: ProblemIndex, decoded: Decoded, previous_plan: PreviousPlan | None, tolerance: float
) -> float:
    """Ops in both plans whose machine changed or whose start moved beyond `tolerance`."""
    if previous_plan is None:
        return 0.0
    changed = 0
    for previous in previous_plan.assignments:
        p = index.pos.get(previous.operation_id)
        if p is None:
            continue
        moved = abs(decoded.starts[p] - previous.start) > tolerance
        if moved or decoded.options[p].machine_id != previous.machine_id:
            changed += 1
    return float(changed)


# --- scorer ------------------------------------------------------------------------------------


class Scorer:
    """Compares objective-value sets like `objectives.rank` (see the module docstring)."""

    __slots__ = ("_reference", "_scales", "_spec")

    def __init__(self, spec: ObjectiveSpec, reference: Values | None) -> None:
        self._spec = spec
        self._reference = reference
        self._scales: tuple[float, ...] = ()
        if isinstance(spec, WeightedSum):
            self._scales = tuple(term_scale(t.kind, t.scale, reference) for t in spec.terms)

    def key(self, values: Values) -> tuple[float, ...]:
        """Sort key: smaller is better (see the module docstring)."""
        spec = self._spec
        if isinstance(spec, WeightedSum):
            return (self._weighted_score(spec, values),)
        if isinstance(spec, Lexicographic):
            return tuple(signed(level.kind, values[level.kind]) for level in spec.levels)
        return _epsilon_key(spec, values)

    def better(self, a: Values, b: Values) -> bool:
        """True when `a` ranks strictly before `b`."""
        return self.delta(a, b) < 0

    def delta(self, a: Values, b: Values) -> float:
        """Negative when `a` is better, positive when `b` is, 0 on a tie."""
        spec = self._spec
        if isinstance(spec, Lexicographic):
            return self._lexicographic_delta(spec, a, b)
        if isinstance(spec, EpsilonConstraint):
            return self._epsilon_delta(spec, a, b)
        return self._weighted_score(spec, a) - self._weighted_score(spec, b)

    def _weighted_score(self, spec: WeightedSum, values: Values) -> float:
        """rank's score, term for term: fsum of weight * signed(value) / scale."""
        return math.fsum(
            term.weight * signed(term.kind, values[term.kind]) / scale
            for term, scale in zip(spec.terms, self._scales, strict=True)
        )

    def _lexicographic_delta(self, spec: Lexicographic, a: Values, b: Values) -> float:
        """The first level where one value lies outside the band of the better one."""
        for level in spec.levels:
            va, vb = signed(level.kind, a[level.kind]), signed(level.kind, b[level.kind])
            best = min(va, vb)
            if max(va, vb) <= best + level_tolerance(level, best):
                continue
            return (va - vb) / self._reference_scale(level.kind)
        return 0.0

    def _epsilon_delta(self, spec: EpsilonConstraint, a: Values, b: Values) -> float:
        violation_a, violation_b = bound_violation(spec, a), bound_violation(spec, b)
        if violation_a > 0 or violation_b > 0:
            return violation_a - violation_b
        kind = spec.primary
        return (signed(kind, a[kind]) - signed(kind, b[kind])) / self._reference_scale(kind)

    def _reference_scale(self, kind: ObjectiveKind) -> float:
        return term_scale(kind, None, self._reference)


def _epsilon_key(spec: EpsilonConstraint, values: Values) -> tuple[float, float]:
    violation = bound_violation(spec, values)
    if violation > 0:
        return (1.0, violation)
    return (0.0, signed(spec.primary, values[spec.primary]))
