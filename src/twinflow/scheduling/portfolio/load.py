"""Bottleneck load ratio and load-dependent objective weights (COMP-023, REQ-119, D-080).

Pinedo p.430: under light load the plant chases due dates, under heavy load it saves setup
time, because capacity is what is short.

- `load_ratio(compiled)`: the max over work centers of
  (sum over the center's pending ops of the shortest run on its machines + n * sbar)
  / (sum over its machines of working seconds in [now, H)), with n and sbar the center's
  `InstanceStats` values (DEV-9), so the numerator is `cmax_hat * m`. H = profile
  `horizon_end`, else problem `horizon_end`, else the max due of a scheduled job; with none
  of these the ratio is None. A center with work but no working seconds in [now, H) has
  ratio `inf` (fully overloaded: the heavy weights apply). Centers without work count 0.
- `effective_objective(profile, ratio)`: with `profile.load_weights`, a `WeightedSum` with
  the light terms' kinds, order and scales and weights (1 - theta) * light + theta * heavy,
  theta = clamp((ratio - light_load) / (heavy_load - light_load), 0, 1); ratio None gives
  the light weights. Without `load_weights`, the profile objective unchanged.
"""

from __future__ import annotations

import math

from twinflow.scheduling.calendar_math import work_seconds
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.model import (
    CompiledProblem,
    LoadWeights,
    ObjectiveSpec,
    SchedulingProfile,
    WeightedSum,
    WeightedTerm,
)


def load_ratio(compiled: CompiledProblem) -> float | None:
    """The bottleneck load ratio of `compiled` (see the module docstring); None without H."""
    index = ProblemIndex.build(compiled.problem)
    horizon = _horizon(compiled, index)
    if horizon is None:
        return None
    ratios = [_center_ratio(index, center, horizon) for center in index.center_machines]
    return max(ratios, default=0.0)


def _horizon(compiled: CompiledProblem, index: ProblemIndex) -> float | None:
    if compiled.profile.horizon_end is not None:
        return compiled.profile.horizon_end
    if compiled.problem.horizon_end is not None:
        return compiled.problem.horizon_end
    return max((job.due for job in index.jobs if job.due is not None), default=None)


def _center_ratio(index: ProblemIndex, center_id: str, horizon: float) -> float:
    stats = InstanceStats.for_center(index, center_id, index.now)
    work = stats.cmax_hat * stats.m
    if work <= 0:
        return 0.0
    capacity = sum(_working_seconds(index, m, horizon) for m in index.machines_of(center_id))
    return work / capacity if capacity > 0 else math.inf


def _working_seconds(index: ProblemIndex, machine_id: str, horizon: float) -> float:
    if horizon <= index.now:
        return 0.0
    machine = index.machines[index.machine_pos[machine_id]]
    return work_seconds(machine.calendar, index.now, horizon)


def effective_objective(profile: SchedulingProfile, ratio: float | None) -> ObjectiveSpec:
    """The profile objective, blended by load when `load_weights` is set (Pinedo p.430)."""
    weights = profile.load_weights
    if weights is None:
        return profile.objective
    theta = _theta(weights, ratio)
    terms = tuple(
        WeightedTerm(
            kind=light.kind,
            weight=(1.0 - theta) * light.weight + theta * heavy.weight,
            scale=light.scale,
        )
        for light, heavy in zip(weights.light, weights.heavy, strict=True)
    )
    return WeightedSum(terms=terms)


def _theta(weights: LoadWeights, ratio: float | None) -> float:
    """Share of the heavy weights: 0 at or below light load (or no ratio), 1 at heavy load."""
    if ratio is None:
        return 0.0
    share = (ratio - weights.light_load) / (weights.heavy_load - weights.light_load)
    return min(1.0, max(0.0, share))
