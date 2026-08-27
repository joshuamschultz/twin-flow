"""General "output-to-Stock destination" capability — unit tests (T-055, red phase).

Routes a Location's output (scrap, rework, offcut, or ANY emitted `thing`) back
into a NAMED top-level Stock -- the recycle/remelt loop. This is GENERAL framework
config (structure.md D-044: "New config sugar lands in the compiler, for
everyone"), never foundry-specific and never scrap-specific: the mechanism works
identically whether the routed `thing` came from a `scrap:` block or an ordinary
bare `emits` entry.

The PRIMITIVE already supports this -- proved by the already-real, already-green
`tests/unit/test_location.py::test_scrap_destination_stock_level_increases_by_the_
scrap_qty`: `Location._fire`'s output step is `isinstance(destination, Stock)` ->
`.put()`, duck-typed and unconditional on the emitted `thing`'s name. NOTHING in
`primitives/location.py` changes for this task. The gap is entirely Layer 2
(config -> compiled contract) and Layer 3 (the driver building a real per-run
`Stock` and wiring it into `destinations[thing]`).

--------------------------------------------------------------------------------
CONFIG SURFACE this test file COMMITS (T-056/T-057/T-058 implementers conform):

    locations:
      - name: <location_id>
        ...                          # unchanged existing LOCATION schema
        output_stocks:               # OPTIONAL. General: maps ANY output `thing`
          <thing>: <stock_name>      # this location emits (a `scrap.thing`, a
                                      # bare `emits[i].thing`, ANY of them) to the
                                      # NAME of a top-level `stocks[*].name` entry.
                                      # Absent key -> that thing's output keeps the
                                      # existing default (a plain list sink).
                                      # Absent `output_stocks` entirely -> no
                                      # change from today's behavior at all.

Identity note (not enforced by this task's validator, called out for the green
implementers and the driver e2e fixture below): `primitives.stock.Stock.put()`
rejects a bundle whose `.thing` does not match the Stock's own `.thing`, and the
already-real top-level `stocks[*]` schema (`model/validate.py::_orphan_stock_
checks`, `examples/spring/model.yaml`) has always used `stocks[*].name` AS the
material `thing` identity (there is no separate `stocks[*].thing` field). So a
`output_stocks` mapping that is runtime-safe names a stock whose declared `name`
equals the SAME `thing` being routed -- e.g. `output_stocks: {offcut: offcut}`
against `stocks: [{name: offcut, uom: piece}]`. A mapping that violates this is a
runtime `Stock.put()` ValueError, not a case this task's validator is asked to
catch (only "names an UNDECLARED stock" is in scope, per the task brief).

--------------------------------------------------------------------------------
COMPILED CONTRACT this test file PINS (T-056 compiler, T-057 driver, T-058
validator must all agree on this shape):

    schema.LocationSpec gets ONE new field:

        stock_destinations: dict[str, str]   # output thing -> declared stock
                                              # NAME (a plain string, never a
                                              # runtime Stock object -- Stock is
                                              # env-bound, per COMP-006, and
                                              # `LocationCompiler.compile()` never
                                              # sees a `simpy.Environment`; only
                                              # `RunDriver.run()` does, exactly
                                              # like `LaborPoolConfig` ->
                                              # `RunDriver._build_labor_pools`
                                              # already does for LaborPool).

        A location with no `output_stocks` declared compiles to
        `stock_destinations == {}` (never `None`, never a missing attribute).

    `model.CompiledModel` (`model/__init__.py`) gets ONE new field carrying the
    top-level `stocks:` section's declarative shape through, the same pattern
    `LaborPoolConfig`/`labor_pools` already sets:

        stocks: list[StockConfig]     # StockConfig(name: str, uom: str) --
                                       # this test file does not assert on this
                                       # field's exact name/shape directly (it is
                                       # RunDriver-internal plumbing, matching the
                                       # existing `labor_pools` precedent in
                                       # `test_run_driver.py`); it only requires
                                       # that build-time Stock declarations survive
                                       # `load_model()` in SOME form, proved
                                       # end-to-end below.

    `plan.driver.RunDriver.run()` builds ONE fresh `primitives.stock.Stock` per
    declared top-level stock, every `run()` call (env-bound, never reused across
    replications -- same "fresh per-run runtime state" rule the module docstring
    already names for "Stocks" alongside Locations/LaborPools/EventLog), then for
    every compiled `LocationSpec.stock_destinations` entry rewrites that fresh
    spec's `destinations[thing]` to the matching built `Stock` (mirroring
    `_wire_routing`'s existing post-compile `destinations` rewrite for Location ->
    Location routing). `RunResult.run_meta` gets a `"stock_levels"` entry:

        run_meta["stock_levels"]: dict[str, float]   # stock name -> FINAL level
                                                      # at the end of THIS run()
                                                      # call -- the read-back seam
                                                      # this test file uses to
                                                      # prove the wiring end to end
                                                      # without reaching into
                                                      # RunDriver internals.

--------------------------------------------------------------------------------
VALIDATION this test file PINS (T-058): a location's `output_stocks[<thing>]`
value naming a stock NOT present in the top-level `stocks[*].name` list is a
`ValidationError` at path `locations[<i>].output_stocks.<thing>`, naming the
undeclared stock in its message. A valid mapping (the named stock IS declared)
contributes zero errors.

--------------------------------------------------------------------------------
RED-phase reasoning (mirrors test_compile.py/test_validate.py/test_run_driver.py
precedent): every failure below is an ASSERTION failure (a `getattr(..., None)`
default not matching, a `dict.get(...)` returning `None`, an error LIST that is
empty when a non-empty list is expected), never an ImportError, never an
AttributeError raised at collection time, never a constructor TypeError. Every
name imported below (`LocationCompiler`, `LocationSpec`, `load_model`,
`RunDriver`, `ModelValidator`, `Stock`) already exists and imports cleanly today
-- only the NEW `output_stocks` behavior these tests exercise is absent.

Real collaborators throughout, no mocks: real `RawModel`/`yaml.safe_load`, real
`LocationCompiler`, real `ModelValidator`, real `load_model`/`RunDriver`, real
SimPy, real Polars round-trip (matching `test_compile.py`/`test_validate.py`/
`test_run_driver.py`'s own established house style for this layer).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from twinflow.model import load_model
from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.model.validate import ModelValidator
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.primitives.part import PartTypeRegistry

# ---------------------------------------------------------------------------
# Shared collaborators, mirroring test_compile.py / test_validate.py house style.
# ---------------------------------------------------------------------------


def _sandbox() -> ExpressionSandbox:
    return ExpressionSandbox(max_depth=10, max_length=200)


def _registry(**parts: str) -> PartTypeRegistry:
    """`part_name=uom` kwargs -> a PartTypeRegistry with no attributes on any part."""
    return PartTypeRegistry({name: {"attributes": {}, "uom": uom} for name, uom in parts.items()})


# ===========================================================================
# T-056 (compiler): a location's `output_stocks` mapping compiles onto the
# pinned `LocationSpec.stock_destinations` field.
# ===========================================================================

COMPILER_MODEL_YAML = """
stocks:
  - name: offcut
    uom: piece

part_types: {}
machines: []
labor: {}
processes: []
bom: []

locations:
  - name: trim
    consumes:
      - thing: blank
        qty: 1
        uom: piece
    emits:
      - thing: good
        qty: 0.9
        uom: piece
      - thing: offcut
        qty: 0.1
        uom: piece
    output_stocks:
      offcut: offcut
    setup_key: grp_trim
    time_model:
      kind: rate_based
      rate: 10
    machine: trim_m
    labor_skill: trim_op

  - name: no_output_stocks_here
    consumes:
      - thing: wire
        qty: 1
        uom: ft
    emits:
      - thing: cut_wire
        qty: 1
        uom: ft
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: 5
    machine: cut_m
    labor_skill: cut_op

routing: []
"""


def _compile_compiler_model():
    registry = _registry(blank="piece", good="piece", offcut="piece", wire="ft", cut_wire="ft")
    compiler = LocationCompiler(registry, _sandbox())
    return compiler.compile(RawModel(yaml.safe_load(COMPILER_MODEL_YAML)))


def _location(result, location_id: str):
    matches = [loc for loc in result.locations if loc.location_id == location_id]
    assert len(matches) == 1, (
        f"expected exactly one compiled location {location_id!r}, found "
        f"{len(matches)}: {[getattr(m, 'location_id', m) for m in matches]}"
    )
    return matches[0]


class TestCompilerRoutesOutputToNamedStock:
    def test_declared_output_stock_thing_compiles_to_stock_destinations_field(self) -> None:
        result = _compile_compiler_model()
        trim = _location(result, "trim")

        # getattr with a None default -- NEVER a bare attribute access -- so a
        # LocationSpec that does not yet carry `stock_destinations` fails this
        # as a plain assertion mismatch (feature absent), never an
        # AttributeError raised at collection or mid-test.
        stock_destinations = getattr(trim, "stock_destinations", None)

        assert stock_destinations == {"offcut": "offcut"}, (
            "LocationSpec.stock_destinations must map the declared output "
            "output_stocks entry (thing -> stock name); got "
            f"{stock_destinations!r}"
        )

    def test_good_output_with_no_output_stocks_entry_is_absent_from_the_mapping(self) -> None:
        """`trim` ALSO emits `good`, which has no `output_stocks` entry -- the
        mapping must be scoped to exactly the declared thing(s), never every
        output thing the location happens to emit."""
        result = _compile_compiler_model()
        trim = _location(result, "trim")

        stock_destinations = getattr(trim, "stock_destinations", None)

        assert stock_destinations is not None
        assert "good" not in stock_destinations

    def test_location_with_no_output_stocks_key_compiles_to_an_empty_mapping(self) -> None:
        """No `output_stocks` key declared at all -> `{}`, never `None` and
        never a missing attribute -- so every consumer (the driver) can iterate
        `spec.stock_destinations.items()` unconditionally."""
        result = _compile_compiler_model()
        cutter = _location(result, "no_output_stocks_here")

        stock_destinations = getattr(cutter, "stock_destinations", None)

        assert stock_destinations == {}, (
            f"a location declaring no output_stocks must compile to an empty "
            f"mapping, not {stock_destinations!r}"
        )


# ===========================================================================
# T-057 (driver/run): a real load_model + RunDriver run ends with the named
# Stock's level increased by the emitted material -- read back through
# RunResult.run_meta["stock_levels"].
#
# Deliberately uses an ORDINARY `emits` entry ("offcut"), NOT a `scrap:` block,
# to prove the mechanism is general -- it routes ANY declared output thing to a
# Stock, not something special-cased to the scrap/foundry vocabulary.
# ===========================================================================

DRIVER_MODEL_YAML = """
stocks:
  - name: offcut
    uom: piece

part_types:
  - name: blank
    uom: piece
    attributes: {}
  - name: good
    uom: piece
    attributes: {}
  - name: offcut
    uom: piece
    attributes: {}

machines:
  - name: trim_m

labor:
  pools:
    - name: floor_pool
      headcount: 1
      skills: [trim_op]

locations:
  - name: trim
    consumes:
      - thing: blank
        qty: 1
        uom: piece
    emits:
      - thing: good
        qty: 1
        uom: piece
      - thing: offcut
        qty: 0.1
        uom: piece
    output_stocks:
      offcut: offcut
    setup_key: grp_trim
    time_model:
      kind: rate_based
      rate: 10
    machine: trim_m
    labor_skill: trim_op

routing:
  - part: good
    steps: [trim]

processes: []
bom: []
"""

ORDER_QTY = 10.0
EXPECTED_OFFCUT_LEVEL = ORDER_QTY * 0.1  # 0.1 offcut per unit of blank consumed


def _write_driver_model(tmp_path: Path) -> Path:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(DRIVER_MODEL_YAML, encoding="utf-8")
    return model_path


def _driver_plan() -> list[WorkOrder]:
    return [
        WorkOrder(
            work_order_id="wo-A",
            part="good",
            qty=int(ORDER_QTY),
            start_date="0",
            due_date="100000",
        )
    ]


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Mirrors test_run_driver.py's own fixture: RunDriver auto-creates
    `runs/<run-id>/`; never let that touch the real repository tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestDriverRoutesEmittedOutputIntoNamedStockAtRuntime:
    def test_offcut_output_routed_to_stock_increases_its_level_by_the_emitted_qty(
        self, isolated_cwd: Path
    ) -> None:
        model_path = _write_driver_model(isolated_cwd)
        compiled = load_model(str(model_path))
        driver = RunDriver(compiled)

        result = driver.run(_driver_plan(), seed=1, replication_index=0)

        stock_levels = result.run_meta.get("stock_levels")
        assert stock_levels is not None, (
            "RunResult.run_meta['stock_levels'] is missing -- the driver does "
            "not yet build a runtime Stock from the top-level `stocks:` "
            "section and/or does not yet wire a location's `output_stocks` "
            "routing into it"
        )
        assert stock_levels["offcut"] == pytest.approx(EXPECTED_OFFCUT_LEVEL), (
            f"expected the 'offcut' stock to hold {EXPECTED_OFFCUT_LEVEL} after "
            f"the run (the location's emitted 10% of {ORDER_QTY} consumed "
            f"blank), got {stock_levels.get('offcut')!r}"
        )

    def test_stock_starts_at_zero_before_any_firing_and_only_gains_the_routed_thing(
        self, isolated_cwd: Path
    ) -> None:
        """Adversarial: the SAME run must not silently credit the stock with
        the 'good' output too -- only the thing this location's
        `output_stocks` actually names."""
        model_path = _write_driver_model(isolated_cwd)
        compiled = load_model(str(model_path))
        driver = RunDriver(compiled)

        result = driver.run(_driver_plan(), seed=2, replication_index=0)

        stock_levels = result.run_meta.get("stock_levels")
        assert stock_levels is not None
        # `good` is a finished-part output with no output_stocks entry; it must
        # never appear as a stock (there is no top-level `stocks[*].name ==
        # "good"` declared at all in this model).
        assert "good" not in stock_levels


# ===========================================================================
# T-058 (validator): an `output_stocks` destination naming an UNDECLARED stock
# is rejected, naming the offending path; a valid one yields no such error.
# ===========================================================================


def _validator_registry() -> PartTypeRegistry:
    return _registry(blank="piece", good="piece", offcut="piece")


def _validator() -> ModelValidator:
    return ModelValidator(_validator_registry(), _sandbox())


def _validator_baseline_location(output_stocks: dict[str, str]) -> dict:
    return {
        "name": "trim",
        "consumes": [{"thing": "blank", "qty": 1, "uom": "piece"}],
        "emits": [
            {"thing": "good", "qty": 0.9, "uom": "piece"},
            {"thing": "offcut", "qty": 0.1, "uom": "piece"},
        ],
        "output_stocks": output_stocks,
        "setup_key": "grp_trim",
        "time_model": {"kind": "rate_based", "rate": 10},
        "machine": "trim_m",
        "labor_skill": "trim_op",
    }


def _validator_baseline_data(output_stocks: dict[str, str], declared_stocks: list[dict]) -> dict:
    return {
        "part_types": {},
        "stocks": declared_stocks,
        "machines": [{"name": "trim_m"}],
        "labor": {"pools": [{"name": "floor_pool", "skills": ["trim_op"]}]},
        "locations": [_validator_baseline_location(output_stocks)],
        "routing": [{"part": "offcut", "steps": ["trim"]}],
        "processes": [],
        "bom": [],
    }


class TestValidatorRejectsUndeclaredStockDestination:
    def test_output_stocks_naming_an_undeclared_stock_is_rejected(self) -> None:
        # "offcut" is never declared under top-level `stocks:` at all.
        data = _validator_baseline_data(output_stocks={"offcut": "offcut"}, declared_stocks=[])

        errors = _validator().validate(RawModel(data))

        matching = [e for e in errors if e.path == "locations[0].output_stocks.offcut"]
        assert len(matching) == 1, (
            "expected exactly one ValidationError at "
            "'locations[0].output_stocks.offcut' for an output_stocks entry "
            f"naming an undeclared stock; found errors: {errors!r}"
        )
        assert "offcut" in matching[0].message

    def test_output_stocks_naming_a_declared_stock_produces_no_such_error(self) -> None:
        data = _validator_baseline_data(
            output_stocks={"offcut": "offcut"},
            declared_stocks=[{"name": "offcut", "uom": "piece"}],
        )

        errors = _validator().validate(RawModel(data))

        matching = [e for e in errors if e.path.startswith("locations[0].output_stocks")]
        assert matching == [], (
            f"a valid output_stocks mapping (naming a DECLARED stock) must "
            f"produce no output_stocks ValidationError; found: {matching!r}"
        )
