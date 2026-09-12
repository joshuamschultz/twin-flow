"""Objectives — turn an `Evaluation` into one scalar an optimizer can rank.

An objective declares a `direction` ("max" or "min") and a `score(evaluation)`; the
optimizer never needs to know which metric it is, only how to compare two scores. Two
stances are first class:

- **point** objectives read the mean of a KPI (on-time %, makespan, utilization);
- **robust** objectives read a confidence-band edge (the *lower* bound of on-time %),
  so a scenario only wins by being reliably good, not luckily good in one draw.

Cost enters as an objective too, via `CostObjective`, and several metrics combine via
`WeightedObjective`. New objectives register in `OBJECTIVES` — no core edit.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from twinflow.modules.costs import CostFunction
from twinflow.modules.registry import Registry
from twinflow.modules.surface import Evaluation

Direction = Literal["max", "min"]


@runtime_checkable
class Objective(Protocol):
    """One scalar score over an `Evaluation`, with a compare direction.

    `name`/`direction` are read-only protocol members (declared as properties),
    so a `frozen=True` dataclass satisfies the protocol with plain fields.
    """

    @property
    def name(self) -> str: ...

    @property
    def direction(self) -> Direction: ...

    def score(self, evaluation: Evaluation) -> float: ...


def is_better(direction: Direction, candidate: float, incumbent: float) -> bool:
    """True when `candidate` beats `incumbent` under `direction`."""
    return candidate > incumbent if direction == "max" else candidate < incumbent


def worst_score(direction: Direction) -> float:
    """The sentinel an optimizer seeds its incumbent with."""
    return float("-inf") if direction == "max" else float("inf")


@dataclass(frozen=True)
class MetricObjective:
    """Score = a named scalar pulled from the evaluation by `extract`."""

    name: str
    direction: Direction
    extract: Callable[[Evaluation], float]

    def score(self, evaluation: Evaluation) -> float:
        return float(self.extract(evaluation))


@dataclass(frozen=True)
class WeightedObjective:
    """A linear blend of other objectives: `sum(weight_i * term_i.score)`.

    Every term is scored in its own natural direction first (a "min" term's score
    is negated so higher is always better inside the blend), then weighted. The
    blend itself is always maximised. Lets a planner say "mostly on-time, but
    penalise cost" in one objective.
    """

    name: str
    terms: tuple[tuple[float, Objective], ...]
    direction: Direction = "max"

    def score(self, evaluation: Evaluation) -> float:
        total = 0.0
        for weight, term in self.terms:
            raw = term.score(evaluation)
            oriented = raw if term.direction == "max" else -raw
            total += weight * oriented
        return total


@dataclass(frozen=True)
class CostObjective:
    """Wrap a `CostFunction` as a minimise objective."""

    name: str
    cost_function: CostFunction
    direction: Direction = "min"

    def score(self, evaluation: Evaluation) -> float:
        return self.cost_function.cost(evaluation)


# ---------------------------------------------------------------------------
# Built-in metric extractors
# ---------------------------------------------------------------------------


def _mean_on_time(evaluation: Evaluation) -> float:
    return evaluation.intervals.on_time_pct.mean


def _robust_on_time(evaluation: Evaluation) -> float:
    """The lower edge of the on-time confidence band — reward reliable, not lucky."""
    return evaluation.intervals.on_time_pct.lo


def _makespan(evaluation: Evaluation) -> float:
    samples = evaluation.per_replication_kpis or (evaluation.kpis,)
    makespans: list[float] = []
    for kpis in samples:
        if any(value is None for value in kpis.completion_by_order.values()):
            return float("inf")
        completions = [value for value in kpis.completion_by_order.values() if value is not None]
        makespans.append(max(completions) if completions else 0.0)
    return sum(makespans) / len(makespans)


def _mean_lateness(evaluation: Evaluation) -> float:
    samples = evaluation.per_replication_kpis or (evaluation.kpis,)
    if any(value is None for kpis in samples for value in kpis.lateness_by_order.values()):
        return float("inf")
    latenesses = [
        value for kpis in samples for value in kpis.lateness_by_order.values() if value is not None
    ]
    return sum(latenesses) / len(latenesses) if latenesses else 0.0


def _mean_utilization(evaluation: Evaluation) -> float:
    samples = evaluation.per_replication_kpis or (evaluation.kpis,)
    cells = [value for kpis in samples for value in kpis.utilization_by_cell.values()]
    return sum(cells) / len(cells) if cells else 0.0


def _service_level(evaluation: Evaluation) -> float:
    """Supply-chain service level as a percentage: 100 minus the average share of
    the run each stock spent empty. A floor with no stocks is trivially 100."""
    stockout = evaluation.inventory.stockout_seconds
    if not stockout or evaluation.horizon <= 0:
        return 100.0
    out_fraction = sum(min(1.0, s / evaluation.horizon) for s in stockout.values())
    return 100.0 * (1.0 - out_fraction / len(stockout))


OBJECTIVES: Registry[Objective] = Registry("objective")
OBJECTIVES.register(
    "on_time_pct",
    lambda: MetricObjective("on_time_pct", "max", _mean_on_time),
)
OBJECTIVES.register(
    "robust_on_time",
    lambda: MetricObjective("robust_on_time", "max", _robust_on_time),
)
OBJECTIVES.register(
    "makespan",
    lambda: MetricObjective("makespan", "min", _makespan),
)
OBJECTIVES.register(
    "mean_lateness",
    lambda: MetricObjective("mean_lateness", "min", _mean_lateness),
)
OBJECTIVES.register(
    "utilization",
    lambda: MetricObjective("utilization", "max", _mean_utilization),
)
OBJECTIVES.register(
    "service_level",
    lambda: MetricObjective("service_level", "max", _service_level),
)
