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
KIND_BATCH_HOLD = "batch_hold"

_VALID_KINDS = frozenset(
    {KIND_DISTRIBUTION, KIND_RATE_BASED, KIND_ATTRIBUTE_SCALED, KIND_BATCH_HOLD}
)


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

    def estimate(self, bundle: Bundle) -> float:
        """A deterministic expected total time for `bundle`, drawing NO randomness —
        the processing-time proxy a dispatch rule (SPT / critical-ratio) orders by.

        Exact for the closed-form kinds: `rate_based` is `qty / rate`,
        `attribute_scaled` is `base * scale(bundle)`, both plus fixed load/unload. A
        `distribution` or `batch_hold` kind has no closed-form mean exposed here, so it
        falls back to `bundle.qty` as a monotone size proxy (documented alpha
        simplification — the ordering, not the absolute value, is what a dispatch rule
        consumes).
        """
        if self.kind == KIND_RATE_BASED:
            rate: float = self.params["rate"]
            base = bundle.qty / rate if rate != 0.0 else bundle.qty
        elif self.kind == KIND_ATTRIBUTE_SCALED:
            attr_base: float = self.params["base"]
            scale: Callable[[Bundle], float] = self.params["scale"]
            base = attr_base * scale(bundle)
        else:
            base = bundle.qty
        return self.load + base + self.unload

    def _run(self, bundle: Bundle, generator: np.random.Generator) -> float:
        if self.kind == KIND_DISTRIBUTION:
            draw: Callable[[np.random.Generator], float] = self.params["draw"]
            return float(draw(generator))

        if self.kind == KIND_BATCH_HOLD:
            # One timed hold over the whole accumulated group at once (an oven,
            # cure, cool): the duration is a property of the operation, not of how
            # many units sit in the batch, so `bundle.qty` is deliberately ignored
            # (the batch is selected as one firing by the `batch_size` PullRule).
            # A fixed `seconds`, or a distribution `draw` for a variable hold.
            hold_draw: Callable[[np.random.Generator], float] | None = self.params.get("draw")
            base = float(hold_draw(generator)) if hold_draw is not None else self.params["seconds"]
            hold_noise: Callable[[np.random.Generator], float] | None = self.params.get("noise")
            return base * float(hold_noise(generator)) if hold_noise is not None else base

        if self.kind == KIND_RATE_BASED:
            rate: float = self.params["rate"]
            if rate == 0.0:
                raise ValueError("rate_based TimeModel requires a nonzero rate")
            base = bundle.qty / rate
        else:
            # KIND_ATTRIBUTE_SCALED — validated as the only remaining option in __init__.
            attr_base: float = self.params["base"]
            scale: Callable[[Bundle], float] = self.params["scale"]
            base = attr_base * scale(bundle)

        # An otherwise-deterministic time model may carry a mean-1 multiplicative
        # `noise` draw (a declared `cv`) so a real floor's spread shows up. Absent
        # it, the time stays exactly deterministic.
        noise: Callable[[np.random.Generator], float] | None = self.params.get("noise")
        return base * float(noise(generator)) if noise is not None else base
