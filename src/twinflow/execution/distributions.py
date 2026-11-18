"""Inverse-CDF quantiles per distribution family (COMP-131, D-092, D-095).

The executor never draws from a `Generator`: it maps one keyed uniform through the
distribution's inverse CDF. So an operation's draw is the same quantile on whichever
machine and in whichever plan it runs (common quantile, D-095).
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import scipy.stats  # type: ignore[import-untyped]  # no stub package; SciPy is the D-036-endorsed library

from twinflow.execution.policy import (
    Deterministic,
    Distribution,
    Empirical,
    LogNormal,
    ShiftedGamma,
    Triangular,
    Uniform,
)

U_MIN = 1e-12
U_MAX = 1 - 1e-12

_STANDARD_NORMAL = NormalDist()


def quantile(dist: Distribution, u: float) -> float:
    """Return the value of `dist` at cumulative probability `u`.

    `u` is clamped to [U_MIN, U_MAX] first, so 0, 1 and values outside [0, 1] map to the
    finite tails. A NaN `u` raises `ValueError` (fail safe: no silent NaN durations).
    """
    if math.isnan(u):
        raise ValueError("quantile needs a number for u, got NaN")
    p = min(max(u, U_MIN), U_MAX)
    match dist:
        case Deterministic():
            return dist.value
        case LogNormal():
            return _lognormal(dist, p)
        case Triangular():
            return _triangular(dist, p)
        case Uniform():
            return dist.low + p * (dist.high - dist.low)
        case ShiftedGamma():
            scale = (dist.mean - dist.minimum) / dist.shape
            return dist.minimum + float(scipy.stats.gamma.ppf(p, dist.shape, scale=scale))
        case Empirical():
            return float(np.quantile(np.asarray(dist.samples), p, method="linear"))


def _lognormal(dist: LogNormal, p: float) -> float:
    """Lognormal with mean and cv; mu and sigma as `model/distributions.py` derives them."""
    if dist.cv == 0.0:
        return dist.mean
    variance = math.log(1.0 + dist.cv * dist.cv)
    sigma = math.sqrt(variance)
    mu = math.log(dist.mean) - variance / 2.0
    return math.exp(mu + sigma * _STANDARD_NORMAL.inv_cdf(p))


def _triangular(dist: Triangular, p: float) -> float:
    """Closed-form inverse CDF of the triangular distribution."""
    width = dist.high - dist.low
    if p < (dist.mode - dist.low) / width:
        return dist.low + math.sqrt(p * width * (dist.mode - dist.low))
    return dist.high - math.sqrt((1.0 - p) * width * (dist.high - dist.mode))
