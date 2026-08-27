"""Capacity-N work center (COMP-030): a Location may declare `capacity: N`, giving
it N identical machines in parallel that jobs pull from. Compiles onto
`LocationSpec.capacity` (default 1); the run driver builds the machine pool at
that size. General framework capability - no vertical name in the engine (D-004).
"""

from __future__ import annotations

import yaml

from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.primitives.part import PartTypeRegistry


def _compile(capacity_line: str):
    model_yaml = f"""
stocks: []
part_types: {{}}
machines: []
labor: {{}}
processes: []
bom: []
locations:
  - name: mill
    consumes:
      - {{thing: raw, qty: 1, uom: piece}}
    emits:
      - {{thing: done, qty: 1, uom: piece}}
    setup_key: g
{capacity_line}
    time_model:
      kind: rate_based
      rate: 10
    machine: m
    labor_skill: s
routing:
  - part: done
    steps: [mill]
"""
    registry = PartTypeRegistry(
        {"raw": {"attributes": {}, "uom": "piece"}, "done": {"attributes": {}, "uom": "piece"}}
    )
    compiler = LocationCompiler(registry, ExpressionSandbox(max_depth=10, max_length=200))
    result = compiler.compile(RawModel(yaml.safe_load(model_yaml)))
    return next(loc for loc in result.locations if loc.location_id == "mill")


def test_capacity_defaults_to_one() -> None:
    assert _compile("").capacity == 1


def test_declared_capacity_compiles_onto_location_spec() -> None:
    assert _compile("    capacity: 3").capacity == 3
