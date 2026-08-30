"""Demand generation — turn a demand description into a production plan.

Today a plan is imported (`.xlsx`/`.csv`); this surface lets one be *generated* from a
product mix and an arrival process, so scenarios can be swept over demand, not only over
levers. Every generator returns the same canonical `list[WorkOrder]` the run driver
already consumes, and every generator is seeded, so a generated plan is reproducible.

A `Forecaster` (see `forecast.py`) can supply the arrival rate or order size a generator
uses — the seam where a demand forecast feeds the twin.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

from twinflow.modules.registry import Registry
from twinflow.plan.loader import WorkOrder


@runtime_checkable
class DemandGenerator(Protocol):
    """Produce a reproducible production plan from a demand description."""

    @property
    def name(self) -> str: ...

    def generate(self, seed: int = 0) -> list[WorkOrder]: ...


@dataclass(frozen=True)
class FixedDemand:
    """`count` orders, evenly spaced over `horizon`, cycling through `parts`.

    The deterministic baseline: same every run. Each order is `qty` units, due
    `lead_time` seconds after its release.
    """

    parts: tuple[str, ...]
    count: int
    qty: int
    horizon: float
    lead_time: float
    name: str = "fixed"

    def generate(self, seed: int = 0) -> list[WorkOrder]:
        if not self.parts:
            raise ValueError("FixedDemand needs at least one part")
        spacing = self.horizon / max(self.count, 1)
        orders: list[WorkOrder] = []
        for index in range(self.count):
            start = index * spacing
            part = self.parts[index % len(self.parts)]
            orders.append(
                WorkOrder(
                    work_order_id=f"gen-{index + 1:04d}",
                    part=part,
                    qty=self.qty,
                    start_date=str(int(start)),
                    due_date=str(int(start + self.lead_time)),
                )
            )
        return orders


@dataclass(frozen=True)
class PoissonDemand:
    """Poisson arrivals at `rate` orders/second over `horizon`, random part + qty.

    Order sizes are drawn uniformly from `[qty_low, qty_high]`. Seeded, so the same
    seed always yields the same plan (the twin's reproducibility contract extends to
    generated demand).
    """

    parts: tuple[str, ...]
    rate: float
    horizon: float
    lead_time: float
    qty_low: int = 1
    qty_high: int = 10
    name: str = "poisson"

    def generate(self, seed: int = 0) -> list[WorkOrder]:
        if not self.parts:
            raise ValueError("PoissonDemand needs at least one part")
        rng = np.random.default_rng(seed)
        orders: list[WorkOrder] = []
        t = 0.0
        index = 0
        while True:
            t += float(rng.exponential(1.0 / self.rate)) if self.rate > 0 else self.horizon + 1
            if t > self.horizon:
                break
            index += 1
            part = self.parts[int(rng.integers(len(self.parts)))]
            qty = int(rng.integers(self.qty_low, self.qty_high + 1))
            orders.append(
                WorkOrder(
                    work_order_id=f"gen-{index:04d}",
                    part=part,
                    qty=qty,
                    start_date=str(int(t)),
                    due_date=str(int(t + self.lead_time)),
                )
            )
        return orders


DEMAND_GENERATORS: Registry[DemandGenerator] = Registry("demand_generator")
DEMAND_GENERATORS.register("fixed", FixedDemand)
DEMAND_GENERATORS.register("poisson", PoissonDemand)
