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

from twinflow.model.distributions import build_draw, build_noise
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.model.schema import (
    LocationSpec,
    MaterialSpec,
    QualityBranchSpec,
    QualityGateSpec,
)
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine, SetupPolicy
from twinflow.primitives.location import PullRule
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock
from twinflow.primitives.time_model import (
    KIND_ATTRIBUTE_SCALED,
    KIND_BATCH_HOLD,
    KIND_DISTRIBUTION,
    KIND_RATE_BASED,
    TimeModel,
)
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
        # A model-level default spread so a real, variable floor is the out-of-the-box
        # behaviour; any location's own `cv` overrides it, and 0.0 stays deterministic.
        defaults = cast(dict[str, Any], raw_model.data.get("defaults", {}))
        default_cv = float(defaults.get("cycle_time_cv", 0.0))
        compiled_locations = [self._compile_location(loc, default_cv) for loc in raw_locations]

        raw_routing = cast(list[dict[str, Any]], raw_model.routing)
        routing = {item["part"]: list(item["steps"]) for item in raw_routing}

        bom = {
            part: _rolled_up_bom(part, steps, locations_by_id) for part, steps in routing.items()
        }

        return CompileResult(locations=compiled_locations, routing=routing, bom=bom)

    def _compile_location(self, loc: RawLocation, default_cv: float = 0.0) -> LocationSpec:
        """Compile one `locations[i]` entry into a LocationSpec, per the committed
        model.yaml LOCATION schema (test_compile.py's docstring is its source of
        truth)."""
        setup_key = loc["setup_key"]
        setup_key_of = {item["thing"]: setup_key for item in loc["consumes"]}

        transform = Transform(_build_transform_spec(loc))
        pull_rule = PullRule(setup_key_of=setup_key_of, spec=loc.get("batch_size"))
        # A declared `changeover_seconds` is the setup/changeover time this center
        # charges when a job's setup group differs from the machine's current one
        # (including the cold-start setup of the first job). Absent means 0.0 — the
        # historical v1 default. Primitives never learn what a changeover is (D-044);
        # the compiler turns the plain-language number into a SetupPolicy.
        changeover_seconds = float(loc.get("changeover_seconds", 0.0))
        setup_policy = SetupPolicy(
            setup_key_of=setup_key_of,
            changeover_matrix={},
            default_seconds=changeover_seconds,
        )

        time_model = _build_time_model(loc["time_model"], default_cv)

        destinations: dict[str, Stock | list[Bundle]] = {
            thing: [] for thing in _all_output_things(loc)
        }

        stock_destinations = {
            str(thing): str(stock_name)
            for thing, stock_name in loc.get("output_stocks", {}).items()
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
            stock_destinations=stock_destinations,
            capacity=int(loc.get("capacity", 1)),
            quality_gate=_build_quality_gate(loc),
            material_spec=_build_material_spec(loc),
        )


def _build_quality_gate(loc: RawLocation) -> QualityGateSpec | None:
    """Compile a declared `quality_gate` into a QualityGateSpec (D-044), or None.

    `thing` defaults to the location's first declared emit (the good output) when
    omitted. Each branch's `to` names a location, a stock, or is null (a terminal
    sink / finished part); RunDriver resolves those into run-bound sinks.
    """
    raw = loc.get("quality_gate")
    if raw is None:
        return None
    thing = str(raw.get("thing") or loc["emits"][0]["thing"])
    branches = [
        QualityBranchSpec(prob=float(branch["prob"]), to=branch.get("to"))
        for branch in raw["branches"]
    ]
    return QualityGateSpec(thing=thing, branches=branches)


def _build_material_spec(loc: RawLocation) -> MaterialSpec | None:
    """Compile a declared secondary-material requirement `material: {stock, qty,
    uom}` into a MaterialSpec (D-044), or None. RunDriver binds it to a Stock."""
    raw = loc.get("material")
    if raw is None:
        return None
    return MaterialSpec(stock=str(raw["stock"]), qty=float(raw["qty"]), uom=str(raw["uom"]))


def _build_time_model(raw: dict[str, Any], default_cv: float = 0.0) -> TimeModel:
    """Compile a declared `time_model` into a pure `TimeModel` (D-044).

    `distribution` becomes a seeded absolute-time `draw`; a `cv` (the location's
    own, else the model-level `default_cv`) on a `rate_based`/`attribute_scaled`
    model becomes a mean-1 multiplicative `noise`. `load`/`unload` pass through as
    fixed phase seconds.
    """
    kind = raw["kind"]
    reserved = {"kind", "load", "unload"}
    load = float(raw.get("load", 0.0))
    unload = float(raw.get("unload", 0.0))
    cv = float(raw.get("cv", default_cv))

    if kind == KIND_DISTRIBUTION:
        spec = {key: value for key, value in raw.items() if key not in reserved}
        params: dict[str, Any] = {"draw": build_draw(spec)}
    elif kind == KIND_BATCH_HOLD:
        # One timed hold over a whole accumulated group (oven/cure/cool). Either a
        # fixed `seconds`, or a distribution (`dist`/`mean`/...) drawn per firing;
        # a `cv` adds multiplicative spread to the fixed form. The hold ignores
        # batch qty by contract (TimeModel._run), so this compiles to a qty-free
        # duration paired with the location's `batch_size` PullRule.
        if "dist" in raw:
            spec = {key: value for key, value in raw.items() if key not in reserved | {"cv"}}
            params = {"draw": build_draw(spec)}
        else:
            params = {"seconds": float(raw["seconds"])}
            if cv > 0.0:
                params["noise"] = build_noise(cv)
    elif kind == KIND_RATE_BASED:
        params = {"rate": raw["rate"]}
        if cv > 0.0:
            params["noise"] = build_noise(cv)
    elif kind == KIND_ATTRIBUTE_SCALED:
        params = {key: value for key, value in raw.items() if key not in reserved | {"cv"}}
        if cv > 0.0:
            params["noise"] = build_noise(cv)
    else:
        # TimeModel.__init__ owns the canonical "unknown kind" error.
        params = {key: value for key, value in raw.items() if key not in reserved}

    return TimeModel(kind=kind, params=params, load=load, unload=unload)


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

    def resolve(thing: str, qty_needed: float, visited: frozenset[str]) -> dict[str, float]:
        if thing not in produced_by:
            return {thing: qty_needed}
        if thing in visited:
            # A rework/scrap cycle (D-055): this thing is produced from material that
            # (transitively) includes itself, e.g. scrap remelted back into the chain.
            # Recycled internal material is not a NEW raw input, so the walk stops here
            # rather than recursing forever. The raw draw enters the chain elsewhere.
            return {}
        loc = locations_by_id[produced_by[thing]]
        consumption = _consumption_per_output_unit(loc, thing)
        next_visited = visited | {thing}
        rolled_up: dict[str, float] = {}
        for sub_thing, per_unit in consumption.items():
            for raw_thing, raw_qty in resolve(
                sub_thing, per_unit * qty_needed, next_visited
            ).items():
                rolled_up[raw_thing] = rolled_up.get(raw_thing, 0.0) + raw_qty
        return rolled_up

    return resolve(finished_part, 1.0, frozenset())
