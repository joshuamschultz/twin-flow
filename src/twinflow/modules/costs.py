"""Cost functions — turn a scenario's outcome into money.

A cost function reads two things off an `Evaluation`: the *configuration* it ran
(headcount, machine capacity — from `compiled`) and the *result* it produced
(lateness — from `kpis`). It never runs the simulator. Costs compose: `TotalCost`
sums any set of them, so "labour + capacity + a penalty for being late" is one object
an optimizer can minimise. New cost functions register in `COSTS`.

Every rate here is a plain assumption the caller supplies (a wage, a machine's hourly
cost, a late-penalty rate); the module invents no numbers of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from twinflow.modules.registry import Registry
from twinflow.modules.surface import Evaluation

_SECONDS_PER_HOUR = 3600.0


@runtime_checkable
class CostFunction(Protocol):
    """A money value for one `Evaluation`.

    `name` is a read-only protocol member (a property), so `frozen=True`
    dataclasses satisfy the protocol with a plain field.
    """

    @property
    def name(self) -> str: ...

    def cost(self, evaluation: Evaluation) -> float: ...


def _makespan_hours(evaluation: Evaluation) -> float:
    """Paid horizon = the last completion, in hours (0 if nothing completed)."""
    completions = [c for c in evaluation.kpis.completion_by_order.values() if c is not None]
    return (max(completions) if completions else 0.0) / _SECONDS_PER_HOUR


@dataclass(frozen=True)
class LaborCost:
    """Total labour spend = headcount * wage/hr * makespan hours, summed over pools."""

    wage_per_hour: float
    name: str = "labor_cost"

    def cost(self, evaluation: Evaluation) -> float:
        hours = _makespan_hours(evaluation)
        headcount = sum(pool.headcount for pool in evaluation.compiled.labor_pools)
        return headcount * self.wage_per_hour * hours


@dataclass(frozen=True)
class CapacityCost:
    """Fixed spend on installed machines = sum(location.capacity) * cost_per_machine.

    A capital/leasing style cost that does not depend on the run's length — it
    prices *owning* the parallel machines a `capacity: N` center declares.
    """

    cost_per_machine: float
    name: str = "capacity_cost"

    def cost(self, evaluation: Evaluation) -> float:
        installed = sum(spec.capacity for spec in evaluation.compiled.locations)
        return installed * self.cost_per_machine


@dataclass(frozen=True)
class LatenessPenalty:
    """A penalty on tardiness = sum(max(0, lateness)) hours * penalty/hr.

    Only positive lateness is charged; finishing early is free (not a bonus).
    """

    penalty_per_hour: float
    name: str = "lateness_penalty"

    def cost(self, evaluation: Evaluation) -> float:
        tardy_seconds = sum(
            max(0.0, v) for v in evaluation.kpis.lateness_by_order.values() if v is not None
        )
        return (tardy_seconds / _SECONDS_PER_HOUR) * self.penalty_per_hour


@dataclass(frozen=True)
class InventoryHoldingCost:
    """Cost of holding inventory = sum(time-weighted average level) * rate/unit/hr *
    horizon hours, over every stock. Prices the capital tied up in on-hand stock."""

    rate_per_unit_hour: float
    name: str = "inventory_holding_cost"

    def cost(self, evaluation: Evaluation) -> float:
        held = sum(evaluation.inventory.average_level.values())
        hours = evaluation.horizon / _SECONDS_PER_HOUR
        return held * self.rate_per_unit_hour * hours


@dataclass(frozen=True)
class StockoutPenalty:
    """Penalty on running out = sum(stockout seconds) hours * penalty/hr, over every
    stock. The cost of a feedstock that sat empty while work waited on it."""

    penalty_per_hour: float
    name: str = "stockout_penalty"

    def cost(self, evaluation: Evaluation) -> float:
        stockout_hours = sum(evaluation.inventory.stockout_seconds.values()) / _SECONDS_PER_HOUR
        return stockout_hours * self.penalty_per_hour


@dataclass(frozen=True)
class TotalCost:
    """The sum of several cost functions — one number to minimise."""

    terms: tuple[CostFunction, ...] = field(default_factory=tuple)
    name: str = "total_cost"

    def cost(self, evaluation: Evaluation) -> float:
        return sum(term.cost(evaluation) for term in self.terms)


COSTS: Registry[CostFunction] = Registry("cost")
COSTS.register("labor_cost", LaborCost)
COSTS.register("capacity_cost", CapacityCost)
COSTS.register("lateness_penalty", LatenessPenalty)
COSTS.register("inventory_holding_cost", InventoryHoldingCost)
COSTS.register("stockout_penalty", StockoutPenalty)
COSTS.register("total_cost", TotalCost)
