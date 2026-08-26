"""COMP-011 TimeModel — cycle time per (Location, part type).

Distribution, rate-based, or attribute-scaled, with an optional load/run/unload split.
Every draw comes from the cycle_time source stream (COMP-003); TimeModel never touches
numpy's global RNG, only the `Generator` handed to `sample()`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

from twinflow.primitives.bundle import Bundle

KIND_DISTRIBUTION = "distribution"
KIND_RATE_BASED = "rate_based"
KIND_ATTRIBUTE_SCALED = "attribute_scaled"

_VALID_KINDS = frozenset({KIND_DISTRIBUTION, KIND_RATE_BASED, KIND_ATTRIBUTE_SCALED})


@dataclass(frozen=True)
class PhaseTimes:
    """Sampled seconds for one location visit: load, run, unload."""

    load: float
    run: float
    unload: float


class TimeModel:
    """Produces PhaseTimes {load, run, unload}; a single value collapses to run."""

    def __init__(
        self,
        kind: str,
        params: dict[str, Any],
        load: float = 0.0,
        unload: float = 0.0,
    ) -> None:
        if kind not in _VALID_KINDS:
            raise ValueError(f"unknown TimeModel kind: {kind!r}")
        self.kind = kind
        self.params = params
        self.load = load
        self.unload = unload

    def sample(self, bundle: Bundle, generator: np.random.Generator) -> PhaseTimes:
        """Sample run time for `bundle` via `generator`; load/unload are fixed."""
        return PhaseTimes(load=self.load, run=self._run(bundle, generator), unload=self.unload)

    def _run(self, bundle: Bundle, generator: np.random.Generator) -> float:
        if self.kind == KIND_DISTRIBUTION:
            draw: Callable[[np.random.Generator], float] = self.params["draw"]
            return float(draw(generator))

        if self.kind == KIND_RATE_BASED:
            rate: float = self.params["rate"]
            if rate == 0.0:
                raise ValueError("rate_based TimeModel requires a nonzero rate")
            return bundle.qty / rate

        # KIND_ATTRIBUTE_SCALED — validated as the only remaining option in __init__.
        base: float = self.params["base"]
        scale: Callable[[Bundle], float] = self.params["scale"]
        return base * scale(bundle)
