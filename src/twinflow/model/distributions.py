"""COMP-038 Distribution builders — Layer 2 config sugar (D-044).

Turns a declared `time_model` distribution (or an optional `cv`) into a pure
`Callable[[numpy.random.Generator], float]`. Primitives never see this module:
they receive the callable and draw from the generator handed to them, which the
run driver sources from `RngRegistry.generator(SOURCE_CYCLE_TIME)` (COMP-003) so
every run reproduces to the number.

Real manufacturing cycle times are positive and right-skewed, so the default
shape is lognormal, parameterised the way a practitioner thinks: a `mean` and a
coefficient of variation `cv` (the standard deviation as a fraction of the mean).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np

Draw = Callable[[np.random.Generator], float]

DIST_LOGNORMAL = "lognormal"
DIST_NORMAL = "normal"
DIST_TRIANGULAR = "triangular"
DIST_UNIFORM = "uniform"
DIST_EXPONENTIAL = "exponential"

_KNOWN = frozenset({DIST_LOGNORMAL, DIST_NORMAL, DIST_TRIANGULAR, DIST_UNIFORM, DIST_EXPONENTIAL})

DEFAULT_DIST = DIST_LOGNORMAL


def build_draw(spec: dict[str, Any]) -> Draw:
    """A seeded absolute-time draw from a declared distribution spec.

    `dist` selects the shape (default lognormal). Shapes and their keys:
      lognormal / normal   mean, cv         (cv is std/mean; normal truncates at 0)
      triangular           low, mode, high
      uniform              low, high
      exponential          mean
    """
    dist = str(spec.get("dist", DEFAULT_DIST))
    if dist not in _KNOWN:
        raise ValueError(f"unknown distribution: {dist!r}")

    if dist == DIST_LOGNORMAL:
        mu, sigma = _lognormal_params(_pos(spec, "mean"), _cv(spec.get("cv", 0.0)))
        return lambda g: float(g.lognormal(mu, sigma))

    if dist == DIST_NORMAL:
        mean, cv = _pos(spec, "mean"), _cv(spec.get("cv", 0.0))
        std = float(spec["std"]) if "std" in spec else mean * cv
        if std < 0.0:
            raise ValueError("normal std must be non-negative")
        return lambda g: max(0.0, float(g.normal(mean, std)))  # a duration is never negative

    if dist == DIST_TRIANGULAR:
        low, mode, high = float(spec["low"]), float(spec["mode"]), float(spec["high"])
        if not low <= mode <= high or low >= high:
            raise ValueError("triangular requires low <= mode <= high and low < high")
        return lambda g: float(g.triangular(low, mode, high))

    if dist == DIST_UNIFORM:
        low, high = float(spec["low"]), float(spec["high"])
        if low >= high:
            raise ValueError("uniform requires low < high")
        return lambda g: float(g.uniform(low, high))

    # DIST_EXPONENTIAL
    mean = _pos(spec, "mean")
    return lambda g: float(g.exponential(mean))


def build_noise(cv: float) -> Draw:
    """A mean-1, positive multiplicative spread for an otherwise-deterministic
    time model. `cv == 0` returns exactly 1.0 every draw, so a model with no
    declared spread stays perfectly reproducible and unchanged."""
    c = _cv(cv)
    if c == 0.0:
        return lambda g: 1.0
    mu, sigma = _lognormal_params(1.0, c)
    return lambda g: float(g.lognormal(mu, sigma))


# -- helpers ---------------------------------------------------------------


def _lognormal_params(mean: float, cv: float) -> tuple[float, float]:
    """Underlying-normal (mu, sigma) giving a lognormal with this mean and cv."""
    variance = math.log(1.0 + cv * cv)
    sigma = math.sqrt(variance)
    mu = math.log(mean) - variance / 2.0
    return mu, sigma


def _cv(value: Any) -> float:
    cv = float(value)
    if cv < 0.0:
        raise ValueError(f"cv must be non-negative, got {cv}")
    return cv


def _pos(spec: dict[str, Any], key: str) -> float:
    value = float(spec[key])
    if value <= 0.0:
        raise ValueError(f"{key} must be positive, got {value}")
    return value
