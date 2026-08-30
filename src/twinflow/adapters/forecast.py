"""Forecasting — the seam a demand/lead-time forecaster plugs into.

Prophet, ARIMA, or a learned model is a separate, later job; this is the interface it
implements (`fit` on a history, `predict` a horizon) so the twin can be *fed* a forecast
without any core change. The forecast feeds demand generation (`demand.py`): the arrival
rate or order size a generator uses can come from a fitted forecaster.

Two numpy-only reference forecasters ship so the surface is usable today.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np

from twinflow.modules.registry import Registry


@runtime_checkable
class Forecaster(Protocol):
    """Fit on a history of observations, then predict the next `periods` values."""

    @property
    def name(self) -> str: ...

    def fit(self, history: Sequence[float]) -> None: ...

    def predict(self, periods: int) -> list[float]: ...


@dataclass
class NaiveForecaster:
    """Predict the last observed value, repeated (the honest baseline every
    forecaster must beat)."""

    name: str = "naive"
    _last: float | None = field(default=None, repr=False)

    def fit(self, history: Sequence[float]) -> None:
        if not history:
            raise ValueError("cannot fit a forecaster on empty history")
        self._last = float(history[-1])

    def predict(self, periods: int) -> list[float]:
        if self._last is None:
            raise RuntimeError("NaiveForecaster.predict called before fit")
        return [self._last] * periods


@dataclass
class MovingAverageForecaster:
    """Predict the mean of the last `window` observations, repeated."""

    window: int = 3
    name: str = "moving_average"
    _mean: float | None = field(default=None, repr=False)

    def fit(self, history: Sequence[float]) -> None:
        if not history:
            raise ValueError("cannot fit a forecaster on empty history")
        tail = np.array(history[-self.window :], dtype=float)
        self._mean = float(tail.mean())

    def predict(self, periods: int) -> list[float]:
        if self._mean is None:
            raise RuntimeError("MovingAverageForecaster.predict called before fit")
        return [self._mean] * periods


FORECASTERS: Registry[Forecaster] = Registry("forecaster")
FORECASTERS.register("naive", NaiveForecaster)
FORECASTERS.register("moving_average", MovingAverageForecaster)
