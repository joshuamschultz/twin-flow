"""TF-004 T-170: inverse-CDF quantiles per distribution family (COMP-131, D-092, D-095).

API fixed by these tests (implementer conforms):

    from twinflow.execution.distributions import quantile, U_MIN, U_MAX

    U_MIN == 1e-12; U_MAX == 1 - 1e-12
    quantile(dist: Distribution, u: float) -> float
        u is clamped to [U_MIN, U_MAX] first (so 0, 1 and values outside [0, 1] are legal).
        Deterministic: value.
        LogNormal(mean, cv): exp(mu + sigma * NormalDist().inv_cdf(u)) with
            sigma = sqrt(log(1 + cv^2)), mu = log(mean) - sigma^2 / 2
            (cv == 0 gives exactly `mean`).
        Triangular(low, mode, high) and Uniform(low, high): closed-form inverse CDF.
        ShiftedGamma(minimum, mean, shape):
            minimum + scipy.stats.gamma.ppf(u, shape, scale=(mean - minimum) / shape).
        Empirical(samples): linear interpolation of the sorted samples at position
            u * (n - 1) (numpy `quantile(..., method="linear")`); one sample is constant.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from scipy import stats

from twinflow.engine.rng import SOURCE_RUN_FACTOR, keyed_uniform
from twinflow.execution.distributions import U_MAX, U_MIN, quantile
from twinflow.execution.policy import (
    Deterministic,
    Empirical,
    LogNormal,
    ShiftedGamma,
    Triangular,
    Uniform,
)

UNIFORMS = tuple(float(u) for u in np.random.default_rng(20261010).uniform(size=200))


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)


def test_clamp_bounds() -> None:
    assert U_MIN == 1e-12
    assert U_MAX == 1 - 1e-12


@pytest.mark.parametrize(("mean", "cv"), [(1.0, 0.15), (1.1, 0.3), (3600.0, 0.5), (1.0, 3.0)])
def test_lognormal_quantile_matches_scipy_ppf(mean: float, cv: float) -> None:
    sigma = math.sqrt(math.log(1 + cv * cv))
    mu = math.log(mean) - sigma * sigma / 2
    reference = stats.lognorm(s=sigma, scale=math.exp(mu))
    dist = LogNormal(mean=mean, cv=cv)
    misses = [u for u in UNIFORMS if not _close(quantile(dist, u), float(reference.ppf(u)))]
    assert misses == []


def test_lognormal_with_zero_cv_is_its_mean() -> None:
    dist = LogNormal(mean=1.25, cv=0.0)
    assert {quantile(dist, u) for u in (0.0, 0.3, 0.999)} == {1.25}


@pytest.mark.parametrize(
    ("low", "mode", "high"), [(0.9, 1.0, 1.3), (0.0, 0.0, 2.0), (0.5, 1.5, 1.5)]
)
def test_triangular_quantile_matches_scipy_ppf(low: float, mode: float, high: float) -> None:
    reference = stats.triang(c=(mode - low) / (high - low), loc=low, scale=high - low)
    dist = Triangular(low=low, mode=mode, high=high)
    misses = [u for u in UNIFORMS if not _close(quantile(dist, u), float(reference.ppf(u)))]
    assert misses == []


@pytest.mark.parametrize(("low", "high"), [(0.9, 1.1), (0.0, 7200.0)])
def test_uniform_quantile_matches_scipy_ppf(low: float, high: float) -> None:
    reference = stats.uniform(loc=low, scale=high - low)
    dist = Uniform(low=low, high=high)
    misses = [u for u in UNIFORMS if not _close(quantile(dist, u), float(reference.ppf(u)))]
    assert misses == []


@pytest.mark.parametrize(
    ("minimum", "mean", "shape"), [(0.9, 1.05, 2.0), (0.0, 1.0, 0.5), (300.0, 900.0, 20.0)]
)
def test_shifted_gamma_quantile_matches_scipy_ppf(
    minimum: float, mean: float, shape: float
) -> None:
    scale = (mean - minimum) / shape
    dist = ShiftedGamma(minimum=minimum, mean=mean, shape=shape)
    misses = [
        u
        for u in UNIFORMS
        if not _close(quantile(dist, u), minimum + float(stats.gamma.ppf(u, shape, scale=scale)))
    ]
    assert misses == []


def test_empirical_quantile_interpolates_sorted_samples() -> None:
    dist = Empirical(samples=(5.0, 1.0, 3.0))
    assert quantile(dist, 0.5) == pytest.approx(3.0, abs=1e-12)
    assert quantile(dist, 0.25) == pytest.approx(2.0, abs=1e-12)
    assert quantile(dist, 0.75) == pytest.approx(4.0, abs=1e-12)
    assert quantile(dist, 0.0) == pytest.approx(1.0, abs=1e-9)
    assert quantile(dist, 1.0) == pytest.approx(5.0, abs=1e-9)


def test_empirical_quantile_matches_numpy_linear_on_200_uniforms() -> None:
    samples = tuple(float(x) for x in np.random.default_rng(7).lognormal(0.0, 0.4, size=57))
    dist = Empirical(samples=samples)
    expected = np.quantile(np.array(samples), np.array(UNIFORMS), method="linear")
    misses = [
        u for u, e in zip(UNIFORMS, expected, strict=True) if not _close(quantile(dist, u), e)
    ]
    assert misses == []


def test_empirical_single_sample_is_constant() -> None:
    dist = Empirical(samples=(2.5,))
    assert {quantile(dist, u) for u in (0.0, 0.5, 1.0)} == {2.5}


@pytest.mark.parametrize("u", [0.0, 1e-300, 0.5, 1.0, 0.999999])
def test_deterministic_one_is_always_one(u: float) -> None:
    assert quantile(Deterministic(value=1.0), u) == 1.0


def test_deterministic_returns_its_value() -> None:
    assert quantile(Deterministic(value=2.5), 0.1) == 2.5


@pytest.mark.parametrize(
    "dist",
    [
        LogNormal(mean=1.0, cv=0.5),
        Triangular(low=0.9, mode=1.0, high=1.3),
        Uniform(low=0.9, high=1.1),
        ShiftedGamma(minimum=0.9, mean=1.05, shape=2),
        Empirical(samples=(1.0, 2.0, 4.0)),
    ],
)
def test_u_is_clamped_to_the_open_interval(
    dist: LogNormal | Triangular | Uniform | ShiftedGamma | Empirical,
) -> None:
    low, high = quantile(dist, U_MIN), quantile(dist, U_MAX)
    assert quantile(dist, 0.0) == low
    assert quantile(dist, -0.5) == low
    assert quantile(dist, 1.0) == high
    assert quantile(dist, 1.5) == high
    assert math.isfinite(low) and math.isfinite(high)
    assert 0.0 <= low < high


def test_lognormal_mean_and_cv_of_100k_keyed_quantiles_within_one_percent() -> None:
    dist = LogNormal(mean=1.1, cv=0.3)
    values = np.array(
        [quantile(dist, keyed_uniform(42, 0, SOURCE_RUN_FACTOR, f"op-{i}")) for i in range(100_000)]
    )
    mean = float(values.mean())
    cv = float(values.std(ddof=1)) / mean
    assert mean == pytest.approx(1.1, rel=0.01)
    assert cv == pytest.approx(0.3, rel=0.01)


def test_quantile_is_monotone_in_u() -> None:
    dist = ShiftedGamma(minimum=0.9, mean=1.05, shape=2)
    ordered = sorted(UNIFORMS)
    values = [quantile(dist, u) for u in ordered]
    assert values == sorted(values)


@pytest.mark.parametrize(
    "dist",
    [
        Deterministic(value=1.0),
        LogNormal(mean=1.0, cv=0.5),
        Triangular(low=0.9, mode=1.0, high=1.3),
        Uniform(low=0.9, high=1.1),
        ShiftedGamma(minimum=0.9, mean=1.05, shape=2),
        Empirical(samples=(1.0, 2.0, 4.0)),
    ],
)
def test_nan_u_is_rejected(
    dist: Deterministic | LogNormal | Triangular | Uniform | ShiftedGamma | Empirical,
) -> None:
    """Fail safe: a NaN uniform never becomes a NaN (or clamped) duration."""
    with pytest.raises(ValueError):
        quantile(dist, math.nan)
