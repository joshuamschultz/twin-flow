"""Config-declared cycle-time variation, end to end through the compiler
(COMP-016 + COMP-038). A declared `distribution` time model, or a `cv` on an
otherwise-deterministic one, must compile into a `TimeModel` whose `sample()`
draws real, seeded, reproducible variation. With no `cv` the model stays
exactly deterministic (existing runs reproduce to the number).
"""

from __future__ import annotations

import numpy as np
import pytest
import yaml

from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.part import PartTypeRegistry


def _compile(time_model_yaml: str):
    model_yaml = f"""
stocks: []
part_types: {{}}
machines: []
labor: {{}}
processes: []
bom: []
locations:
  - name: op
    consumes:
      - {{thing: raw, qty: 1, uom: piece}}
    emits:
      - {{thing: done, qty: 1, uom: piece}}
    setup_key: g
    time_model:
{time_model_yaml}
    machine: m
    labor_skill: s
routing:
  - part: done
    steps: [op]
"""
    registry = PartTypeRegistry(
        {"raw": {"attributes": {}, "uom": "piece"}, "done": {"attributes": {}, "uom": "piece"}}
    )
    compiler = LocationCompiler(registry, ExpressionSandbox(max_depth=10, max_length=200))
    result = compiler.compile(RawModel(yaml.safe_load(model_yaml)))
    return next(loc for loc in result.locations if loc.location_id == "op").time_model


def _runs(time_model, qty: float, n: int = 20000, seed: int = 3) -> np.ndarray:
    g = np.random.default_rng(seed)
    bundle = Bundle(qty=qty, thing="raw", uom="piece")
    return np.array([time_model.sample(bundle, g).run for _ in range(n)])


def test_distribution_time_model_compiles_to_seeded_variation() -> None:
    tm = _compile("      kind: distribution\n      dist: lognormal\n      mean: 100\n      cv: 0.3")
    s = _runs(tm, qty=1.0)
    assert (s > 0).all()
    assert s.mean() == pytest.approx(100.0, rel=0.03)
    assert (s.std() / s.mean()) == pytest.approx(0.3, rel=0.08)


def test_rate_based_with_cv_varies_around_the_deterministic_time() -> None:
    tm = _compile("      kind: rate_based\n      rate: 10\n      cv: 0.2")
    s = _runs(tm, qty=100.0)  # deterministic base = 100 / 10 = 10 s
    assert (s > 0).all()
    assert s.mean() == pytest.approx(10.0, rel=0.03)
    assert (s.std() / s.mean()) == pytest.approx(0.2, rel=0.08)


def test_rate_based_without_cv_stays_exactly_deterministic() -> None:
    tm = _compile("      kind: rate_based\n      rate: 10")
    s = _runs(tm, qty=100.0, n=500)
    assert (s == 10.0).all()


def test_compiled_draw_is_reproducible() -> None:
    tm = _compile("      kind: distribution\n      mean: 50\n      cv: 0.25")
    a = _runs(tm, qty=1.0, n=300, seed=99)
    b = _runs(tm, qty=1.0, n=300, seed=99)
    assert list(a) == list(b)


def _compile_with_defaults(defaults_yaml: str, cv_line: str):
    """Compile a single rate_based location under a model-level `defaults` block."""
    model_yaml = f"""
{defaults_yaml}
stocks: []
part_types: {{}}
machines: []
labor: {{}}
processes: []
bom: []
locations:
  - name: op
    consumes:
      - {{thing: raw, qty: 1, uom: piece}}
    emits:
      - {{thing: done, qty: 1, uom: piece}}
    setup_key: g
    time_model:
      kind: rate_based
      rate: 10
{cv_line}
    machine: m
    labor_skill: s
routing:
  - part: done
    steps: [op]
"""
    registry = PartTypeRegistry(
        {"raw": {"attributes": {}, "uom": "piece"}, "done": {"attributes": {}, "uom": "piece"}}
    )
    compiler = LocationCompiler(registry, ExpressionSandbox(max_depth=10, max_length=200))
    result = compiler.compile(RawModel(yaml.safe_load(model_yaml)))
    return next(loc for loc in result.locations if loc.location_id == "op").time_model


def test_model_default_cv_gives_every_time_model_variation() -> None:
    tm = _compile_with_defaults("defaults:\n  cycle_time_cv: 0.2", "")
    s = _runs(tm, qty=100.0)
    assert s.mean() == pytest.approx(10.0, rel=0.03)
    assert (s.std() / s.mean()) == pytest.approx(0.2, rel=0.08)


def test_location_cv_overrides_the_model_default() -> None:
    tm = _compile_with_defaults("defaults:\n  cycle_time_cv: 0.2", "      cv: 0.5")
    s = _runs(tm, qty=100.0)
    assert (s.std() / s.mean()) == pytest.approx(0.5, rel=0.08)


def test_no_default_and_no_cv_stays_deterministic() -> None:
    tm = _compile_with_defaults("", "")
    s = _runs(tm, qty=100.0, n=300)
    assert (s == 10.0).all()
