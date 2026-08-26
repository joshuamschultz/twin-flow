"""Regression: `_rolled_up_bom` must not recurse forever on a rework/scrap cycle.

The ModelValidator permits a routing/scrap cycle when a `max_rework` bound is
declared (D-055, `tests/unit/test_validate.py`), but the LocationCompiler's BOM
rollup walked the producer graph with no visited-set guard, so any such cycle
crashed `load_model`/`twinflow run` with `RecursionError` even though `validate`
passed. Surfaced building the foundry worked example (T-049): scrap from `clean`
is remelted at `melt`, which closes a producer cycle.

The rolled-up bill must resolve to true raw inputs only; recycled internal material
(the scrap looped back into melt) contributes no NEW raw and must terminate the walk.
"""

from __future__ import annotations

from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.primitives.part import PartTypeRegistry


def _registry() -> PartTypeRegistry:
    def spec(uom: str) -> dict[str, object]:
        return {"attributes": {}, "uom": uom}

    return PartTypeRegistry(
        {
            "raw_metal": spec("lb"),
            "molten": spec("lb"),
            "casting": spec("piece"),
            "finished": spec("piece"),
            "scrap": spec("lb"),
        }
    )


def _sandbox() -> ExpressionSandbox:
    return ExpressionSandbox(max_depth=10, max_length=200)


def _cyclic_foundry() -> RawModel:
    """melt (consumes raw_metal + recycled scrap) -> pour -> clean (emits finished + scrap);
    the scrap clean emits is consumed back at melt, closing a producer cycle."""
    return RawModel(
        {
            "part_types": {},
            "stocks": [],
            "machines": [{"name": "m1"}, {"name": "m2"}, {"name": "m3"}],
            "labor": {"pools": [{"name": "pool", "skills": ["op"], "headcount": 1}]},
            "locations": [
                {
                    "name": "melt",
                    "consumes": [
                        {"thing": "raw_metal", "qty": 1, "uom": "lb"},
                        {"thing": "scrap", "qty": 0.1, "uom": "lb"},
                    ],
                    "emits": [{"thing": "molten", "qty": 1, "uom": "lb"}],
                    "time_model": {"kind": "rate_based", "rate": 5},
                    "setup_key": "g1",
                    "machine": "m1",
                    "labor_skill": "op",
                    "max_rework": 3,
                },
                {
                    "name": "pour",
                    "consumes": [{"thing": "molten", "qty": 1, "uom": "lb"}],
                    "emits": [{"thing": "casting", "qty": 1, "uom": "piece"}],
                    "time_model": {"kind": "rate_based", "rate": 5},
                    "setup_key": "g2",
                    "machine": "m2",
                    "labor_skill": "op",
                },
                {
                    "name": "clean",
                    "consumes": [{"thing": "casting", "qty": 1, "uom": "piece"}],
                    "emits": [
                        {"thing": "finished", "qty": 1, "uom": "piece"},
                        {"thing": "scrap", "qty": 0.05, "uom": "lb"},
                    ],
                    "time_model": {"kind": "rate_based", "rate": 5},
                    "setup_key": "g3",
                    "machine": "m3",
                    "labor_skill": "op",
                },
            ],
            "routing": [{"part": "finished", "steps": ["melt", "pour", "clean"]}],
        }
    )


def test_compile_terminates_on_a_rework_cycle_and_rolls_up_to_raw_only() -> None:
    result = LocationCompiler(_registry(), _sandbox()).compile(_cyclic_foundry())

    bom = result.bom["finished"]
    # The only true raw input is raw_metal; recycled scrap and intermediate things
    # (molten, casting) are produced within the chain and must resolve away.
    assert "raw_metal" in bom
    assert bom["raw_metal"] > 0
    assert "scrap" not in bom
    assert "molten" not in bom
    assert "casting" not in bom
