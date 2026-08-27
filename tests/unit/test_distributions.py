"""Layer-2 distribution builders (`model/distributions.py`) — the config growth
point (D-044) that turns a declared `time_model` distribution into a seeded
`draw(generator) -> float`, and an optional `cv` into a mean-1 multiplicative
`noise(generator) -> float`. Primitives stay pure: they receive callables, never
config (structure.md, test_time_labor.py contract).

Every draw MUST come from the handed generator (COMP-003 / SOURCE_CYCLE_TIME),
never numpy's global RNG, so the same seed reproduces the same run to the number.
"""

from __future__ import annotations

import numpy as np
import pytest

from twinflow.model.distributions import build_draw, build_noise


def _gen(seed: int = 0) -> np.random.Generator:
    return np.random.default_rng(seed)


def _samples(draw, n: int = 20000, seed: int = 7) -> np.ndarray:
    g = _gen(seed)
    return np.array([draw(g) for _ in range(n)])


# -- lognormal is the realistic default -------------------------------------


def test_omitted_dist_defaults_to_lognormal() -> None:
    """A distribution spec with no `dist` key models a real floor: positive,
    right-skewed cycle times centered on the declared mean."""
    draw = build_draw({"mean": 120.0, "cv": 0.25})
    s = _samples(draw)
    assert (s > 0).all()  # lognormal never goes negative
    assert s.mean() == pytest.approx(120.0, rel=0.03)
    assert (s.std() / s.mean()) == pytest.approx(0.25, rel=0.08)
    # right-skewed: mean sits above the median
    assert s.mean() > float(np.median(s))


def test_lognormal_mean_and_cv_match_declared() -> None:
    draw = build_draw({"dist": "lognormal", "mean": 50.0, "cv": 0.4})
    s = _samples(draw)
    assert s.mean() == pytest.approx(50.0, rel=0.03)
    assert (s.std() / s.mean()) == pytest.approx(0.4, rel=0.08)


# -- other shapes -----------------------------------------------------------


def test_triangular_respects_low_mode_high() -> None:
    draw = build_draw({"dist": "triangular", "low": 10.0, "mode": 20.0, "high": 60.0})
    s = _samples(draw)
    assert s.min() >= 10.0 and s.max() <= 60.0
    assert s.mean() == pytest.approx((10.0 + 20.0 + 60.0) / 3.0, rel=0.03)


def test_uniform_spans_low_high() -> None:
    draw = build_draw({"dist": "uniform", "low": 5.0, "high": 15.0})
    s = _samples(draw)
    assert s.min() >= 5.0 and s.max() <= 15.0
    assert s.mean() == pytest.approx(10.0, rel=0.02)


def test_normal_is_truncated_nonnegative() -> None:
    """A normal cycle time must never hand back a negative duration."""
    draw = build_draw({"dist": "normal", "mean": 3.0, "cv": 1.5})
    s = _samples(draw)
    assert (s >= 0).all()


# -- reproducibility --------------------------------------------------------


def test_draw_is_reproducible_for_a_fixed_seed() -> None:
    draw = build_draw({"mean": 100.0, "cv": 0.3})
    assert list(_samples(draw, n=200, seed=42)) == list(_samples(draw, n=200, seed=42))


# -- multiplicative noise (rate_based / attribute_scaled spread) ------------


def test_noise_is_mean_one_and_positive() -> None:
    noise = build_noise(0.2)
    s = _samples(noise)
    assert (s > 0).all()
    assert s.mean() == pytest.approx(1.0, rel=0.02)
    assert (s.std() / s.mean()) == pytest.approx(0.2, rel=0.08)


def test_zero_cv_noise_is_exactly_one() -> None:
    """cv == 0 keeps a model deterministic (existing runs reproduce exactly)."""
    noise = build_noise(0.0)
    g = _gen(1)
    assert all(noise(g) == 1.0 for _ in range(100))


# -- rejects nonsense -------------------------------------------------------


def test_unknown_distribution_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown distribution"):
        build_draw({"dist": "wishful", "mean": 1.0})


def test_negative_cv_is_rejected() -> None:
    with pytest.raises(ValueError, match="cv"):
        build_noise(-0.1)
