"""COMP-016 LocationCompiler — plain-language Location config -> transform/location specs.

The single growth point for new config sugar, and therefore the seam that replaces a
plugin system. Primitives never learn what a scrap rate is (D-044).

Every ratio in the YAML surface (a plain `emits[i].qty`, or a `scrap.rate` split) is
declared relative to `consumes[0]`, the "basis" input. The compiled `Transform`'s
output qty callables re-derive that ratio against the ACTUAL qty of `consumes[0].thing`
present in the bundles a firing consumes at runtime — never a value baked in at
compile time — which is what lets one firing consume 40 units and another 2.5 ft
through the identical compiled spec.

The BOM rollup (`_rolled_up_bom`) walks the same declared ratios backward, offline,
to answer "how much raw material does one finished part need", resolving any
`consumes[*].thing` that another location in the same routing chain produces down to
true raw materials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.model.schema import LocationSpec
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine, SetupPolicy
from twinflow.primitives.location import PullRule
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock
from twinflow.primitives.time_model import TimeModel
from twinflow.primitives.transform import OutputSpec, QtyFn, Transform, TransformSpec

RawLocation = dict[str, Any]


@dataclass(frozen=True)
class CompileResult:
    """LocationCompiler's output: compiled locations, the routing graph, and the BOM."""

    locations: list[LocationSpec]
    routing: dict[str, list[str]]
    bom: dict[str, dict[str, float]]


class LocationCompiler:
    """RawModel parse tree -> list[LocationSpec] + routing graph + BOM rollup."""

    def __init__(self, registry: PartTypeRegistry, sandbox: ExpressionSandbox) -> None:
        self._registry = registry
        self._sandbox = sandbox

    def compile(self, raw_model: RawModel) -> CompileResult:
        """Compile every declared location, the routing graph, and the rolled-up BOM."""
        raw_locations = cast(list[RawLocation], raw_model.locations)
        locations_by_id = {loc["name"]: loc for loc in raw_locations}
        compiled_locations = [self._compile_location(loc) for loc in raw_locations]

        raw_routing = cast(list[dict[str, Any]], raw_model.routing)
        routing = {item["part"]: list(item["steps"]) for item in raw_routing}

        bom = {
            part: _rolled_up_bom(part, steps, locations_by_id) for part, steps in routing.items()
        }

        return CompileResult(locations=compiled_locations, routing=routing, bom=bom)

    def _compile_location(self, loc: RawLocation) -> LocationSpec:
        """Compile one `locations[i]` entry into a LocationSpec, per the committed
        model.yaml LOCATION schema (test_compile.py's docstring is its source of
        truth)."""
        setup_key = loc["setup_key"]
        setup_key_of = {item["thing"]: setup_key for item in loc["consumes"]}

        transform = Transform(_build_transform_spec(loc))
        pull_rule = PullRule(setup_key_of=setup_key_of, spec=loc.get("batch_size"))
        setup_policy = SetupPolicy(
            setup_key_of=setup_key_of, changeover_matrix={}, default_seconds=0.0
        )

        time_model_raw = loc["time_model"]
        time_model = TimeModel(
            kind=time_model_raw["kind"],
            params={key: value for key, value in time_model_raw.items() if key != "kind"},
        )

        destinations: dict[str, Stock | list[Bundle]] = {
            thing: [] for thing in _all_output_things(loc)
        }

        return LocationSpec(
            location_id=loc["name"],
            machine=Machine(machine_id=loc["machine"]),
            setup_policy=setup_policy,
            pull_rule=pull_rule,
            time_model=time_model,
            transform=transform,
            registry=self._registry,
            labor_skill=loc["labor_skill"],
            material_requirement=None,
            destinations=destinations,
        )


def _all_output_things(loc: RawLocation) -> list[str]:
    """Every `thing` this location emits, including the synthesized scrap output."""
    things = [emit["thing"] for emit in loc["emits"]]
    scrap = loc.get("scrap")
    if scrap is not None:
        things.append(scrap["thing"])
    return things


def _output_uom(loc: RawLocation, thing: str) -> str:
    """The declared uom for one output `thing` — from `emits` or the `scrap` block."""
    scrap = loc.get("scrap")
    if scrap is not None and thing == scrap["thing"]:
        return str(scrap["uom"])
    for emit in loc["emits"]:
        if emit["thing"] == thing:
            return str(emit["uom"])
    raise KeyError(f"location {loc['name']!r} does not produce {thing!r}")


def _declared_output_ratio(loc: RawLocation, output_thing: str) -> float:
    """Declared units of `output_thing` per one actual unit of `consumes[0].thing`.

    A plain `emits[i].qty` is declared relative to `consumes[0].qty` (the ratio
    basis), so the ratio is `qty / basis_qty`. A `scrap` block synthesizes two
    ratios directly against the actual basis qty (no basis-qty division): `rate`
    for `scrap.thing`, `1 - rate` for the one `emits` entry that omits `qty`.
    """
    basis_qty = float(loc["consumes"][0]["qty"])
    scrap = loc.get("scrap")
    if scrap is not None and output_thing == scrap["thing"]:
        return float(scrap["rate"])
    for emit in loc["emits"]:
        if emit["thing"] != output_thing:
            continue
        qty = emit.get("qty")
        if qty is not None:
            return float(qty) / basis_qty
        if scrap is not None:
            return 1.0 - float(scrap["rate"])
        raise ValueError(
            f"location {loc['name']!r}: emits entry for {output_thing!r} has no qty "
            "and no scrap block is declared to supply the remainder"
        )
    raise KeyError(f"location {loc['name']!r} does not produce {output_thing!r}")


def _ratio_qty_fn(ratio: float, basis_thing: str) -> QtyFn:
    """A QtyFn that scales `ratio` against the ACTUAL qty of `basis_thing` in the
    inputs a firing consumes at runtime — never a value baked in at compile time."""

    def qty_fn(inputs: list[Bundle]) -> float:
        actual_basis_qty = sum(bundle.qty for bundle in inputs if bundle.thing == basis_thing)
        return ratio * actual_basis_qty

    return qty_fn


def _build_transform_spec(loc: RawLocation) -> TransformSpec:
    """Compile `consumes`/`emits`/`scrap` into the one bundles-in/bundles-out
    contract (D-043, D-044) — no hand-written emit expression anywhere."""
    basis_thing = loc["consumes"][0]["thing"]
    outputs = [
        OutputSpec(
            thing=thing,
            uom=_output_uom(loc, thing),
            qty=_ratio_qty_fn(_declared_output_ratio(loc, thing), basis_thing),
        )
        for thing in _all_output_things(loc)
    ]
    return TransformSpec(outputs=outputs)


def _consumption_per_output_unit(loc: RawLocation, output_thing: str) -> dict[str, float]:
    """Declared qty of each `consumes[*].thing` needed to produce ONE unit of
    `output_thing`, per this location's own recipe ratios."""
    consumes = loc["consumes"]
    basis_qty = float(consumes[0]["qty"])
    output_ratio = _declared_output_ratio(loc, output_thing)
    consumption: dict[str, float] = {}
    for item in consumes:
        per_basis_unit = float(item["qty"]) / basis_qty
        consumption[item["thing"]] = per_basis_unit / output_ratio
    return consumption


def _rolled_up_bom(
    finished_part: str, steps: list[str], locations_by_id: dict[str, RawLocation]
) -> dict[str, float]:
    """Roll up operation-level consumption along `steps` into raw materials only.

    Any `consumes[*].thing` produced by another location within this same routing
    chain is resolved away recursively; a `thing` no location in the chain produces
    is a raw material and terminates the recursion.
    """
    produced_by: dict[str, str] = {}
    for location_id in steps:
        loc = locations_by_id[location_id]
        for thing in _all_output_things(loc):
            produced_by[thing] = location_id

    def resolve(thing: str, qty_needed: float) -> dict[str, float]:
        if thing not in produced_by:
            return {thing: qty_needed}
        loc = locations_by_id[produced_by[thing]]
        consumption = _consumption_per_output_unit(loc, thing)
        rolled_up: dict[str, float] = {}
        for sub_thing, per_unit in consumption.items():
            for raw_thing, raw_qty in resolve(sub_thing, per_unit * qty_needed).items():
                rolled_up[raw_thing] = rolled_up.get(raw_thing, 0.0) + raw_qty
        return rolled_up

    return resolve(finished_part, 1.0)
