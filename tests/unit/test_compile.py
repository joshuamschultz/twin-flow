"""COMP-016 LocationCompiler — unit tests (T-026, red phase).

LocationCompiler (`model/compile.py`) is the single growth point for new config
sugar (structure.md D-044, "Two levels: surface and mechanism"): the YAML surface
is plain floor language — time, scrap rate, batch size, what goes in, what comes
out — and the compiler is the ONLY place that sugar lands. Primitives never learn
what a "scrap rate" is; they only ever see a compiled `Transform` (bundles in,
bundles out), a `PullRule`, a `TimeModel`, a `Machine`, a `SetupPolicy`.

This is also the only place the plain-language `model.yaml` LOCATION schema is
defined. No prior task committed one, so THIS test file is the schema's source of
truth; the T-027 implementer conforms to it. It is intentionally small and boring.

--------------------------------------------------------------------------------
COMMITTED model.yaml LOCATION SCHEMA (this test file fixes it):

    locations:
      - name: <location_id str>              # -> LocationSpec.location_id
        consumes:                             # -> the location's own recipe;
          - thing: <part id str>              #    ALSO the operation-level input
            qty: <float>                      #    that COMP-016 rolls up into the
            uom: <str>                        #    finished part's BOM (criterion 4).
          - thing: ...                        #    consumes[0] is the "primary" input:
            qty: ...                          #    its declared qty is the ratio BASIS
            uom: ...                          #    every emits[i].qty (or the scrap
                                               #    split) scales against, relative to
                                               #    the ACTUAL qty of consumes[0].thing
                                               #    present in the bundles a firing
                                               #    consumes at runtime.
        emits:                                # -> output bundle spec(s)
          - thing: <part id str>
            qty: <float, OPTIONAL>             # ratio of output per one unit of
            uom: <str>                         # consumes[0].qty. REQUIRED unless a
                                                # `scrap` block is declared (below),
                                                # in which case exactly one `emits`
                                                # entry may omit qty: it becomes the
                                                # "good" (non-scrap) remainder.
        scrap:                                 # OPTIONAL. When present, the compiler
          rate: <float, 0..1>                  # SYNTHESIZES the 95/5-style split with
          thing: <part id str>                 # no hand-written emit expression in
          uom: <str>                           # config (D-044): good gets
                                                # (1 - rate) of consumes[0]'s actual
                                                # qty, `scrap.thing` gets `rate` of it.
        batch_size: "<qty> <uom>", OPTIONAL     # e.g. "200 piece" -> PullRule
                                                # threshold, same string grammar as
                                                # the already-real PullRule spec
                                                # (primitives/location.py). Absent ->
                                                # PullRule.default_applied is True
                                                # (arrival order, D-037/D-040).
        setup_key: <str>                       # -> SetupPolicy.setup_key_of and
                                                # PullRule.setup_key_of, for every
                                                # `thing` this location consumes.
        time_model:                            # -> primitives.time_model.TimeModel
          kind: rate_based | distribution | attribute_scaled
          rate: <float>                        # kind-specific params, unchanged
                                                # shape from TimeModel's own __init__.
        machine: <machine_id str>               # -> Machine(machine_id=...)
        labor_skill: <str>                      # -> LocationSpec.labor_skill, verbatim

    routing:
      - part: <finished part id str>
        steps: [<location_id str>, ...]         # ordered location ids; reused
                                                 # verbatim from the already-committed
                                                 # test_loader.py routing shape.

Two locations may share a `thing` between one's `emits` and the next's `consumes`
(e.g. `cutter` emits `blank`, `assembler` consumes `blank`) — that shared identity,
walked backward along a `routing[...].steps` chain, is exactly how criterion 4's
BOM rollup finds raw materials behind an intermediate part.

--------------------------------------------------------------------------------
COMMITTED COMPILER API (this test file fixes it; the T-027 implementer conforms):

    compiler = LocationCompiler(registry: PartTypeRegistry, sandbox: ExpressionSandbox)
    result = compiler.compile(raw_model: RawModel)

    result.locations: list[LocationSpec]        # LocationSpecLike-conforming objects
    result.routing: dict[str, list[str]]         # finished part -> ordered location ids
                                                  # (compiled straight from raw routing)
    result.bom: dict[str, dict[str, float]]      # finished part -> {raw material thing:
                                                  # qty per one unit of finished part},
                                                  # rolled up through every intermediate
                                                  # part along the routing chain.

Each `LocationSpec` in `result.locations` is expected to satisfy
`primitives.location.LocationSpecLike` (already-real Protocol): `.location_id`,
`.machine` (a real `Machine`), `.setup_policy` (a real `SetupPolicy`), `.pull_rule`
(a real `PullRule`), `.time_model` (a real `TimeModel`), `.transform` (a real
`Transform`), `.registry`, `.labor_skill`, `.material_requirement`, `.destinations`.
No concrete `LocationSpec` class is imported here — src does not have one yet
(T-027 has not landed) — assertions below are purely structural/duck-typed, exactly
like the established precedent in `tests/unit/test_location.py`.

--------------------------------------------------------------------------------
RED-phase note (mirrors test_location.py's precedent exactly): `LocationCompiler`
currently has `__init__(self) -> None: raise NotImplementedError("T-027")` — it
takes NO constructor arguments. Every test below constructs it as
`LocationCompiler(registry, sandbox)`, the full designed 2-argument signature. At
RED that raises `TypeError` (signature mismatch — too many positional arguments
for the stub) before Python ever reaches the stub body's `NotImplementedError`
line. Both are the RIGHT reason to fail per this task's own rule ("stub raises
NotImplementedError / signature mismatch — feature absent — not ImportError or
syntax"): `LocationCompiler` imports cleanly from `factory_twin.model.compile`
today; only calling it fails, and only because the real behavior does not exist.

Pure Layer 2 tests: real `RawModel` (parsed via `yaml.safe_load`, matching
`model/loader.py`'s own contract), real `PartTypeRegistry`, real
`ExpressionSandbox`. No mocks. Assertions run the COMPILED `Transform.apply(...)`
and `PullRule.select(...)` — real Layer 1 primitive behavior — rather than
inspecting compiler-internal state.
"""

from __future__ import annotations

import pytest
import yaml

from factory_twin.model.compile import LocationCompiler
from factory_twin.model.expressions import ExpressionSandbox
from factory_twin.model.loader import RawModel
from factory_twin.primitives.bundle import Bundle
from factory_twin.primitives.location import PullRule
from factory_twin.primitives.part import PartTypeRegistry

# ---------------------------------------------------------------------------
# One shared model.yaml exercising every acceptance case:
#   - `press`     : scrap-rate synthesis (criterion 1) + a declared batch_size
#                   (criterion 2)
#   - `cutter`    : plain consumes/emits ratio, no scrap, no batch_size
#                   (criterion 3, and the "arrival order default" half of
#                   criterion 2)
#   - `assembler` : multiple consumes entries feeding one emit (criterion 3)
#   - `cutter` -> `assembler`, routed as the `widget` finished part (criterion 4:
#     BOM rollup through the intermediate `blank` part to the raw materials
#     `wire` and `glue`)
# ---------------------------------------------------------------------------

MODEL_YAML = """
stocks: []
part_types: {}
machines: []
labor: {}
processes: []
bom: []

locations:
  - name: press
    consumes:
      - thing: raw_blank
        qty: 1
        uom: piece
    emits:
      - thing: good
        uom: piece
    scrap:
      rate: 0.05
      thing: scrap_bits
      uom: piece
    batch_size: "200 piece"
    setup_key: grp_press
    time_model:
      kind: rate_based
      rate: 12
    machine: press_m
    labor_skill: press_op

  - name: cutter
    consumes:
      - thing: wire
        qty: 1
        uom: ft
    emits:
      - thing: blank
        qty: 10
        uom: piece
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: 5
    machine: cutter_m
    labor_skill: cut_op

  - name: assembler
    consumes:
      - thing: blank
        qty: 1
        uom: piece
      - thing: glue
        qty: 0.2
        uom: lb
    emits:
      - thing: widget
        qty: 1
        uom: piece
    setup_key: grp_asm
    time_model:
      kind: rate_based
      rate: 8
    machine: asm_m
    labor_skill: asm_op

routing:
  - part: widget
    steps: [cutter, assembler]
"""


def _raw_model() -> RawModel:
    return RawModel(yaml.safe_load(MODEL_YAML))


def _registry() -> PartTypeRegistry:
    def spec(uom: str) -> dict:
        return {"attributes": {}, "uom": uom}

    return PartTypeRegistry(
        {
            "raw_blank": spec("piece"),
            "good": spec("piece"),
            "scrap_bits": spec("piece"),
            "wire": spec("ft"),
            "blank": spec("piece"),
            "glue": spec("lb"),
            "widget": spec("piece"),
        }
    )


def _sandbox() -> ExpressionSandbox:
    return ExpressionSandbox(max_depth=10, max_length=200)


def _compile():
    """Build the compiler with real collaborators and compile the shared model."""
    compiler = LocationCompiler(_registry(), _sandbox())
    return compiler.compile(_raw_model())


def _location(result, location_id: str):
    """Find the one compiled LocationSpec with this location_id, or fail loudly."""
    matches = [loc for loc in result.locations if loc.location_id == location_id]
    assert len(matches) == 1, (
        f"expected exactly one compiled location {location_id!r}, "
        f"found {len(matches)}: {[getattr(m, 'location_id', m) for m in matches]}"
    )
    return matches[0]


# ---------------------------------------------------------------------------
# Acceptance 1 — scrap 5% synthesizes 95 good + 5 scrap, no hand-written
# emit expression anywhere in `press`'s config.
# ---------------------------------------------------------------------------


class TestScrapRateSynthesizesGoodScrapSplit:
    def test_100_units_in_splits_into_95_good_and_5_scrap(self) -> None:
        result = _compile()
        press = _location(result, "press")

        inputs = [Bundle(qty=100.0, thing="raw_blank", uom="piece")]
        outputs = press.transform.apply(inputs, _registry())

        by_thing = {bundle.thing: bundle for bundle in outputs}
        assert set(by_thing) == {"good", "scrap_bits"}
        assert by_thing["good"].qty == pytest.approx(95.0)
        assert by_thing["good"].uom == "piece"
        assert by_thing["scrap_bits"].qty == pytest.approx(5.0)
        assert by_thing["scrap_bits"].uom == "piece"

    def test_scrap_split_scales_with_actual_consumed_qty_not_hardcoded(self) -> None:
        """A fake 'always 95/5' implementation must fail this: 40 units in splits
        38/2, never 95/5. Proves the split is computed from the input, not literal."""
        result = _compile()
        press = _location(result, "press")

        inputs = [Bundle(qty=40.0, thing="raw_blank", uom="piece")]
        outputs = press.transform.apply(inputs, _registry())

        by_thing = {bundle.thing: bundle for bundle in outputs}
        assert by_thing["good"].qty == pytest.approx(38.0)
        assert by_thing["scrap_bits"].qty == pytest.approx(2.0)

    def test_destinations_cover_every_declared_output_thing(self) -> None:
        """LocationSpecLike requires `.destinations` keyed by every output `thing`
        (primitives/location.py). With no stock declared for either output, both
        route to the 'onward' sink list, mirroring test_location.py's precedent."""
        result = _compile()
        press = _location(result, "press")

        assert isinstance(press.destinations, dict)
        assert set(press.destinations) == {"good", "scrap_bits"}
        assert press.destinations["good"] == []
        assert press.destinations["scrap_bits"] == []


# ---------------------------------------------------------------------------
# Acceptance 2 — declared batch_size compiles into a PullRule threshold;
# an undeclared batch_size keeps arrival-order default.
# ---------------------------------------------------------------------------


class TestBatchSizeCompilesIntoPullRule:
    def test_declared_batch_size_sets_pull_rule_threshold(self) -> None:
        result = _compile()
        press = _location(result, "press")

        assert isinstance(press.pull_rule, PullRule)
        assert press.pull_rule.default_applied is False

        queue = [
            Bundle(qty=100.0, thing="raw_blank", uom="piece"),
            Bundle(qty=100.0, thing="raw_blank", uom="piece"),
            Bundle(qty=100.0, thing="raw_blank", uom="piece"),
        ]
        # Threshold is 200 piece: accumulates until running total >= 200, so
        # the first two 100-piece bundles are selected and the third is not
        # (PullRule.select's already-real accumulate-then-stop semantics).
        selected = press.pull_rule.select(queue, current_setup="grp_press")
        assert len(selected) == 2
        assert sum(bundle.qty for bundle in selected) == pytest.approx(200.0)

    def test_no_declared_batch_size_defaults_to_arrival_order(self) -> None:
        result = _compile()
        cutter = _location(result, "cutter")

        assert isinstance(cutter.pull_rule, PullRule)
        assert cutter.pull_rule.default_applied is True

        queue = [Bundle(qty=1.0, thing="wire", uom="ft")]
        # Arrival-order default selects every eligible bundle, unconditionally.
        selected = cutter.pull_rule.select(queue, current_setup="grp_cut")
        assert selected == queue


# ---------------------------------------------------------------------------
# Acceptance 3 — consumes/emits compile into the Transform's input/output
# bundle specs.
# ---------------------------------------------------------------------------


class TestConsumesEmitsCompileIntoTransformBundleSpecs:
    def test_declared_ratio_produces_matching_output_bundle(self) -> None:
        result = _compile()
        cutter = _location(result, "cutter")

        inputs = [Bundle(qty=1.0, thing="wire", uom="ft")]
        outputs = cutter.transform.apply(inputs, _registry())

        assert len(outputs) == 1
        assert outputs[0].thing == "blank"
        assert outputs[0].uom == "piece"
        assert outputs[0].qty == pytest.approx(10.0)

    def test_ratio_scales_with_actual_consumed_qty_not_hardcoded(self) -> None:
        """A fake 'always emit 10' implementation must fail this: 2.5 ft of wire
        (2.5x the declared consumes[0].qty basis) emits 25 blank, not 10."""
        result = _compile()
        cutter = _location(result, "cutter")

        inputs = [Bundle(qty=2.5, thing="wire", uom="ft")]
        outputs = cutter.transform.apply(inputs, _registry())

        assert outputs[0].qty == pytest.approx(25.0)

    def test_multiple_consumes_entries_feed_one_emit(self) -> None:
        result = _compile()
        assembler = _location(result, "assembler")

        inputs = [
            Bundle(qty=1.0, thing="blank", uom="piece"),
            Bundle(qty=0.2, thing="glue", uom="lb"),
        ]
        outputs = assembler.transform.apply(inputs, _registry())

        assert len(outputs) == 1
        assert outputs[0].thing == "widget"
        assert outputs[0].uom == "piece"
        assert outputs[0].qty == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Acceptance 4 — operation-level consumption rolls up into a BOM: `assembler`
# consumes `blank` (an intermediate part `cutter` produces from `wire`) plus
# raw `glue` directly. The rolled-up BOM for the finished part `widget` must
# express BOTH in terms of raw materials only, with `blank` resolved away.
# ---------------------------------------------------------------------------


class TestOperationLevelConsumptionRollsUpIntoBOM:
    def test_routing_graph_compiles_the_declared_step_order(self) -> None:
        result = _compile()
        assert result.routing["widget"] == ["cutter", "assembler"]

    def test_bom_rolls_up_through_the_intermediate_part_to_raw_materials(self) -> None:
        result = _compile()

        # assembler: 0.2 lb glue direct, plus 1 blank -> rolled up through cutter
        # (1 wire ft -> 10 blank, i.e. 0.1 wire ft per blank) -> 0.1 wire ft.
        # `blank` itself must NOT appear in the rolled-up BOM: it is an
        # intermediate part produced on the floor, not a raw material.
        bom = result.bom["widget"]
        assert set(bom) == {"wire", "glue"}
        assert bom["wire"] == pytest.approx(0.1)
        assert bom["glue"] == pytest.approx(0.2)


# ---------------------------------------------------------------------------
# Supporting coverage — the rest of the enumerated config surface (machine,
# setup, labor, time) actually lands on the compiled LocationSpec.
# ---------------------------------------------------------------------------


class TestRemainingConfigSurfaceCompiles:
    def test_machine_setup_labor_time_compile_onto_the_location_spec(self) -> None:
        result = _compile()
        press = _location(result, "press")

        assert press.machine.machine_id == "press_m"
        assert press.labor_skill == "press_op"
        assert press.time_model.kind == "rate_based"
        assert press.time_model.params["rate"] == pytest.approx(12.0)
        assert press.setup_policy.setup_key_of["raw_blank"] == "grp_press"
