"""Tier 0 — batch/hold operation (one timed hold over a whole accumulated group).

A `time_model: {kind: batch_hold, seconds: N}` charges ONE hold duration for the
firing, independent of how many units the `batch_size` PullRule accumulated into
it. Contrast rate_based, where the run time scales with `bundle.qty`.
"""

from __future__ import annotations

import numpy as np

from twinflow.model.compile import _build_time_model
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.time_model import KIND_BATCH_HOLD, TimeModel


def _gen() -> np.random.Generator:
    return np.random.default_rng(0)


def test_batch_hold_duration_ignores_batch_qty() -> None:
    model = TimeModel(kind=KIND_BATCH_HOLD, params={"seconds": 300.0})
    small = model.sample(Bundle(qty=1, thing="x", uom="piece"), _gen())
    large = model.sample(Bundle(qty=500, thing="x", uom="piece"), _gen())
    assert small.run == 300.0
    assert large.run == 300.0  # one hold over the whole group, not per unit


def test_batch_hold_compiles_fixed_seconds() -> None:
    model = _build_time_model({"kind": "batch_hold", "seconds": 120})
    assert model.kind == KIND_BATCH_HOLD
    assert model.sample(Bundle(qty=42, thing="x", uom="piece"), _gen()).run == 120.0


def test_batch_hold_compiles_distribution_and_is_seeded() -> None:
    model = _build_time_model({"kind": "batch_hold", "dist": "lognormal", "mean": 200.0, "cv": 0.3})
    a = model.sample(Bundle(qty=1, thing="x", uom="piece"), np.random.default_rng(7))
    b = model.sample(Bundle(qty=1, thing="x", uom="piece"), np.random.default_rng(7))
    assert a.run == b.run  # reproducible from the same seed
    assert a.run > 0.0
