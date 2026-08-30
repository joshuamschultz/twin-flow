"""LeverSpace — the search domain every optimizer and objective ranges over.

A lever is one dot-path into `model.yaml` (the same scheme `SweepHarness` uses, e.g.
`labor.pools[0].headcount` or `locations[mill_shaft].capacity`). A domain gives that
lever its candidate values: a bounded integer range (staffing, capacity, buffer size)
or an explicit choice set (a categorical knob). The space knows how to enumerate its
grid, draw a random point, and list a point's near neighbours — the three primitives
grid search, random search, and hill climbing are built from.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from itertools import product
from typing import Any

import numpy as np


@dataclass(frozen=True)
class IntRange:
    """An inclusive integer lever domain `[low, high]` stepped by `step`.

    The natural domain for staffing, machine capacity, and buffer/batch sizes —
    the levers a planner actually turns. `values()` materialises the grid,
    `sample()` draws one uniformly, `clamp()` snaps an out-of-range value back
    onto the nearest valid step (used by mutation/crossover in the optimizers).
    """

    low: int
    high: int
    step: int = 1

    def __post_init__(self) -> None:
        if self.step < 1:
            raise ValueError(f"IntRange step must be >= 1, got {self.step}")
        if self.high < self.low:
            raise ValueError(f"IntRange high {self.high} is below low {self.low}")

    def values(self) -> list[int]:
        return list(range(self.low, self.high + 1, self.step))

    def sample(self, rng: np.random.Generator) -> int:
        choices = self.values()
        return int(choices[rng.integers(len(choices))])

    def clamp(self, value: int) -> int:
        stepped = self.low + round((value - self.low) / self.step) * self.step
        return int(min(self.high, max(self.low, stepped)))

    def neighbors(self, value: int) -> list[int]:
        """The value one step below and one step above, clamped into range."""
        found = {self.clamp(value - self.step), self.clamp(value + self.step)}
        found.discard(value)
        return sorted(found)


@dataclass(frozen=True)
class Choice:
    """An explicit, unordered set of candidate values for one lever."""

    options: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("Choice must declare at least one option")

    def values(self) -> list[Any]:
        return list(self.options)

    def sample(self, rng: np.random.Generator) -> Any:
        return self.options[rng.integers(len(self.options))]

    def clamp(self, value: Any) -> Any:
        return value if value in self.options else self.options[0]

    def neighbors(self, value: Any) -> list[Any]:
        return [option for option in self.options if option != value]


Domain = IntRange | Choice


@dataclass(frozen=True)
class LeverSpace:
    """A named set of levers, each with its candidate domain."""

    domains: dict[str, Domain]

    def __post_init__(self) -> None:
        if not self.domains:
            raise ValueError("LeverSpace declares no levers")

    def grid(self) -> Iterator[dict[str, Any]]:
        """Every point in the cartesian product of the levers' candidate values.
        Enumeration order is stable (dict insertion order), so a bounded grid
        search takes a reproducible prefix."""
        keys = list(self.domains)
        for combo in product(*(self.domains[key].values() for key in keys)):
            yield dict(zip(keys, combo, strict=True))

    def grid_size(self) -> int:
        size = 1
        for domain in self.domains.values():
            size *= len(domain.values())
        return size

    def sample(self, rng: np.random.Generator) -> dict[str, Any]:
        """One random point: each lever drawn independently from its domain."""
        return {key: domain.sample(rng) for key, domain in self.domains.items()}

    def neighbors(self, point: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Every point differing from `point` in exactly one lever by one step."""
        for key, domain in self.domains.items():
            current = point[key]
            for candidate in domain.neighbors(current):
                neighbour = dict(point)
                neighbour[key] = candidate
                yield neighbour

    def clamp(self, point: dict[str, Any]) -> dict[str, Any]:
        """Snap every lever of `point` back onto a valid value in its domain."""
        return {key: domain.clamp(point[key]) for key, domain in self.domains.items()}
