"""Robustness measures of a plan (COMP-137, REQ-137, D-100, D-101; Pinedo Sec 18.1).

- `planned_robustness`: R(S) read from `objectives.evaluate` (Pinedo p.483).
- `delta_curve`: deterministic replays of the plan, machine orders fixed, with one
  scripted outage of length delta at `now` on each probe machine (the top planned
  bottlenecks); each point reports Z'(delta), the increase over the planned Z and the
  slope (Pinedo measure 1, D-100).
- `expected_objective`: Z + sum p_delta (Z'(delta) - Z) over the worst probe per delta.
- `load_check`: planned busy share per machine per week from `now` against the weekly
  targets (Pinedo rule (i), p.485-486, D-101).

Every objective value comes from `objectives.evaluate`; working time from `calendar_math`
(through `metrics.busy_seconds`). No file or network I/O.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from twinflow.execution import engine, metrics
from twinflow.execution.policy import ScriptedOutage, SimulationPolicy
from twinflow.execution.shop import ShopModel
from twinflow.execution.sources import DeterministicSource
from twinflow.scheduling import objectives
from twinflow.scheduling.calendar_math import work_seconds
from twinflow.scheduling.model import CompiledProblem, ObjectiveKind, Schedule

WEEK_SECONDS = 7 * 86_400.0
MAX_LISTED_VIOLATIONS = 100
PROBE_SEED = 0
"""Seed of the probe replays; the deterministic source draws nothing, so any value works."""


@dataclass(frozen=True, slots=True)
class DeltaPoint:
    """One probe replay: an outage of `delta_seconds` at `now` on `machine_id` (D-100)."""

    delta_seconds: float
    machine_id: str
    objective: float
    increase: float
    slope: float


@dataclass(frozen=True, slots=True)
class LoadViolation:
    """A machine whose planned load in week `week` exceeds `target` (D-101)."""

    machine_id: str
    week: int
    load: float
    target: float


@dataclass(frozen=True, slots=True)
class LoadCheck:
    """The weekly load check: weeks covered, every violation counted, the worst excess and
    the first `MAX_LISTED_VIOLATIONS` violations by excess, descending (D-101)."""

    weeks: int
    violations_total: int
    worst_excess: float
    violations: tuple[LoadViolation, ...]


@dataclass(frozen=True, slots=True)
class RobustnessSummary:
    """The COMP-137 output for one plan."""

    planned_r: float
    delta_curve: tuple[DeltaPoint, ...]
    expected_objective: float | None
    load_check: LoadCheck


def planned_robustness(values: Mapping[ObjectiveKind, float]) -> float:
    """R(S) = sum w_j (d_j - C_j) / sum w_j (d_j - now), from `objectives.evaluate` values."""
    return values[ObjectiveKind.ROBUSTNESS]


def probe_machines(shop: ShopModel, plan: Schedule, count: int) -> tuple[str, ...]:
    """The top `count` machines by planned busy share, ties by id (never more than exist)."""
    shares = metrics.busy_shares(shop, metrics.plan_occupation(plan), metrics.plan_end(shop, plan))
    ranked = sorted(shares, key=lambda machine_id: (-shares[machine_id], machine_id))
    return tuple(ranked[:count])


def delta_curve(
    shop: ShopModel, compiled: CompiledProblem, plan: Schedule, policy: SimulationPolicy
) -> tuple[DeltaPoint, ...]:
    """Probe replays in grid order, then probe order (D-100).

    Each replay is deterministic whatever the policy's source (`DeterministicSource`, no
    breakdowns, no profile outages), runs the plan with machine orders fixed
    (`PlanControl`) and applies exactly one scripted outage `(m, now, delta, 1.0)`.
    """
    kind = policy.robustness_objective
    planned_z = objectives.evaluate(compiled, plan)[kind]
    probes = probe_machines(shop, plan, policy.robustness_probe_machines)
    limits = engine.Limits.for_shop(shop, metrics.plan_end(shop, plan) - shop.now)
    return tuple(
        _probe(shop, compiled, plan, limits, kind, planned_z, machine_id, delta)
        for delta in policy.robustness_deltas_seconds
        for machine_id in probes
    )


def _probe(
    shop: ShopModel,
    compiled: CompiledProblem,
    plan: Schedule,
    limits: engine.Limits,
    kind: ObjectiveKind,
    planned_z: float,
    machine_id: str,
    delta: float,
) -> DeltaPoint:
    run = engine.execute(
        shop,
        engine.PlanControl(plan),
        draws=engine.RepDraws(seed=PROBE_SEED, replication=0, source=DeterministicSource()),
        outages=(ScriptedOutage(machine_id=machine_id, start=shop.now, duration=delta),),
        limits=limits,
    )
    z_prime = objectives.evaluate(compiled, metrics.realized_schedule(run.records))[kind]
    increase = z_prime - planned_z
    return DeltaPoint(
        delta_seconds=delta,
        machine_id=machine_id,
        objective=z_prime,
        increase=increase,
        slope=increase / delta,
    )


def headline(points: Sequence[DeltaPoint]) -> tuple[DeltaPoint, ...]:
    """The worst probe per delta (largest increase, ties by machine id), in grid order."""
    worst: dict[float, DeltaPoint] = {}
    for point in points:
        current = worst.get(point.delta_seconds)
        if current is None or _worse(point, current):
            worst[point.delta_seconds] = point
    return tuple(worst.values())


def _worse(point: DeltaPoint, other: DeltaPoint) -> bool:
    return (-point.increase, point.machine_id) < (-other.increase, other.machine_id)


def expected_objective(
    z: float, points: Sequence[DeltaPoint], probabilities: Sequence[float] | None
) -> float | None:
    """Z + sum p_delta (Z'(delta) - Z) over the headline points; None without
    probabilities (D-100). `probabilities[i]` belongs to the i-th delta of the grid."""
    if probabilities is None:
        return None
    worst = headline(points)
    if len(worst) != len(probabilities):
        raise ValueError("one probability per delta of the grid is required")
    return z + math.fsum(p * point.increase for p, point in zip(probabilities, worst, strict=True))


def load_check(shop: ShopModel, plan: Schedule, targets: Sequence[float]) -> LoadCheck:
    """Planned load per machine and 7-day week from `now` against `targets` (D-101).

    Load = planned busy working seconds in the week / working seconds of the machine's
    calendar in the week (always working without a calendar). Weeks without working time
    are skipped; week k uses `targets[min(k, len(targets) - 1)]`.
    """
    if not targets:
        raise ValueError("at least one load target is required")
    end = metrics.plan_end(shop, plan)
    weeks = math.ceil((end - shop.now) / WEEK_SECONDS) if end > shop.now else 0
    occupation = metrics.plan_occupation(plan)
    violations: list[LoadViolation] = []
    for week in range(weeks):
        target = targets[min(week, len(targets) - 1)]
        violations.extend(_week_violations(shop, occupation, week, target))
    violations.sort(key=lambda v: (-(v.load - v.target), v.machine_id, v.week))
    return LoadCheck(
        weeks=weeks,
        violations_total=len(violations),
        worst_excess=violations[0].load - violations[0].target if violations else 0.0,
        violations=tuple(violations[:MAX_LISTED_VIOLATIONS]),
    )


def _week_violations(
    shop: ShopModel,
    occupation: Mapping[str, Sequence[tuple[float, float]]],
    week: int,
    target: float,
) -> list[LoadViolation]:
    start = shop.now + week * WEEK_SECONDS
    end = start + WEEK_SECONDS
    found: list[LoadViolation] = []
    for machine_id, machine in shop.machines.items():
        working = work_seconds(machine.calendar, start, end)
        if working <= 0:
            continue
        busy = metrics.busy_seconds(machine.calendar, occupation.get(machine_id, ()), start, end)
        load = busy / working
        if load > target:
            found.append(LoadViolation(machine_id, week, load, target))
    return found


def robustness_summary(
    shop: ShopModel,
    compiled: CompiledProblem,
    plan: Schedule,
    policy: SimulationPolicy,
    targets: Sequence[float],
) -> RobustnessSummary:
    """Planned R(S), the delta curve, the expected objective and the load check of `plan`."""
    values = objectives.evaluate(compiled, plan)
    curve = delta_curve(shop, compiled, plan, policy)
    return RobustnessSummary(
        planned_r=planned_robustness(values),
        delta_curve=curve,
        expected_objective=expected_objective(
            values[policy.robustness_objective], curve, policy.robustness_delta_probabilities
        ),
        load_check=load_check(shop, plan, targets),
    )


__all__ = [
    "DeltaPoint",
    "LoadCheck",
    "LoadViolation",
    "RobustnessSummary",
    "delta_curve",
    "expected_objective",
    "headline",
    "load_check",
    "planned_robustness",
    "probe_machines",
    "robustness_summary",
]
