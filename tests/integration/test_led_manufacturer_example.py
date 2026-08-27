"""LED MANUFACTURER worked example — integration test.

`examples/led-manufacturer/model.yaml` + `plan.csv` is a made-to-order LED
light maker expressed purely as config, no Python: diffuser sheet -> CUT
(continuous-to-discrete, mirrors examples/spring/'s wire cut) -> panel piece
-> ASSEMBLE (several component bundles -> one light, mirrors
test_compile.py's `assembler` case: panel + led_strip + driver + housing all
feed ONE emit) -> BURN-IN -> PACK -> finished packed light, for three size
variants (small/medium/large) standing in for made-to-order sizing (see
examples/led-manufacturer/README.md for the exact plan-schema gap that
blocks true per-order continuous dimensions).

This file exercises the example exactly as a client would run it:
`load_model` -> `validate_model` -> `twinflow.cli.main.main(["run", ...])`
(the same path `twinflow run` takes on the command line) -> a readable event
log and `kpis.json`. It also proves the ASSEMBLE recipe's own math directly
against the compiled `Transform` (cheap, real collaborators, no mocks,
mirroring test_spring_example.py's direct-Transform pattern for the CUT
step): multiple consumes bundles (panel + led_strip + driver + housing) feed
exactly one `light` emit, scaled off the panel (the declared ratio basis).

THE HEADLINE STORY (post engine-correction, D-034 labor-shortage tuning): a
work center now pulls one job at a time by default, and the report's wait
breakdown is per-CENTER idle time, so a labor shortage and a machine
bottleneck read differently in the KPIs -- this example is tuned to read as
the former. `assembler_pool` (headcount 1, skill `asm_op`) is the only pool
that can staff ANY `assemble_*` station, so all three size variants'
assembly steps compete for the SAME one operator, while `support_pool`
(headcount 6) keeps cut/burn-in/pack fast and ample on purpose (see
model.yaml comments). `test_labor_pool_is_the_constraint_the_machines_have_headroom`
proves this from a REAL in-process run (`RunDriver` + `KpiEngine`, no
CLI/JSON round-trip), using the exact call shape production code
(`SweepHarness._kpi_rows_for_point`) already uses: `orders_frame` +
`KpiEngine().compute`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import polars as pl
import pytest

from twinflow.cli.main import main
from twinflow.instrumentation.kpis import KpiEngine
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.part import PartTypeRegistry

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "led-manufacturer"
MODEL_PATH = EXAMPLE_DIR / "model.yaml"
PLAN_PATH = EXAMPLE_DIR / "plan.csv"

SIZES = ("small", "medium", "large")


def _plan_total_qty_by_part(path: Path) -> dict[str, float]:
    """Total demanded qty per finished part, read straight from the plan CSV."""
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    totals: dict[str, float] = {}
    for row in rows:
        totals[row["part"]] = totals.get(row["part"], 0.0) + float(row["qty"])
    return totals


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp_path so `runs/<run-id>/` never touches the repo tree."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run_dir_names(cwd: Path) -> set[str]:
    runs_dir = cwd / "runs"
    if not runs_dir.exists():
        return set()
    return {entry.name for entry in runs_dir.iterdir() if entry.is_dir()}


def test_model_validates_with_no_errors() -> None:
    errors = validate_model(str(MODEL_PATH))

    assert errors == []


def test_model_loads_and_routes_every_size_to_its_packed_finished_part() -> None:
    compiled = load_model(str(MODEL_PATH))

    assert compiled.routing["packed_light_small"] == [
        "cut_small",
        "assemble_small",
        "burnin_small",
        "pack_small",
    ]
    assert compiled.routing["packed_light_medium"] == [
        "cut_medium",
        "assemble_medium",
        "burnin_medium",
        "pack_medium",
    ]
    assert compiled.routing["packed_light_large"] == [
        "cut_large",
        "assemble_large",
        "burnin_large",
        "pack_large",
    ]

    # BOM rolls each size's whole chain up to raw materials only: every
    # intermediate part (panel, light, tested_light) resolves away.
    assert set(compiled.bom["packed_light_small"]) == {
        "diffuser_sheet",
        "led_strip",
        "driver",
        "housing_small",
    }


def test_cut_emits_the_expected_panel_count_plus_a_leftover_sheet_remainder() -> None:
    """CUT's continuous-to-discrete math, proven directly against the compiled
    Transform (mirrors test_spring_example.py's cut test): 3 ft of diffuser
    sheet in yields the expected panel COUNT plus a leftover-sheet remainder,
    both ordinary emits ratios against the same consumes[0] basis."""
    compiled = load_model(str(MODEL_PATH))
    cut_small = next(loc for loc in compiled.locations if loc.location_id == "cut_small")

    inputs = [Bundle(qty=3.0, thing="diffuser_sheet", uom="ft")]
    outputs = cut_small.transform.apply(inputs, compiled.registry)

    by_thing = {bundle.thing: bundle for bundle in outputs}
    assert set(by_thing) == {"panel_small", "sheet_remainder"}
    assert by_thing["panel_small"].qty == pytest.approx(3.0 * 4)
    assert by_thing["panel_small"].uom == "piece"
    assert by_thing["sheet_remainder"].qty == pytest.approx(3.0 * 0.05)
    assert by_thing["sheet_remainder"].uom == "ft"


def test_assemble_consumes_multiple_components_into_one_light() -> None:
    """The ASSEMBLY signature step: panel + led_strip + driver + housing (four
    consumes entries) feed exactly one `light_small` emit, scaled off the
    panel (consumes[0], the declared ratio basis) -- proven directly against
    the compiled Transform, cheap and deterministic, avoiding any live-run
    firing-order dependency (see README.md's PullRule finding)."""
    compiled = load_model(str(MODEL_PATH))
    assemble_small = next(loc for loc in compiled.locations if loc.location_id == "assemble_small")

    inputs = [
        Bundle(qty=1.0, thing="panel_small", uom="piece"),
        Bundle(qty=2.0, thing="led_strip", uom="ft"),
        Bundle(qty=1.0, thing="driver", uom="piece"),
        Bundle(qty=1.0, thing="housing_small", uom="piece"),
    ]
    outputs = assemble_small.transform.apply(inputs, compiled.registry)

    assert len(outputs) == 1
    assert outputs[0].thing == "light_small"
    assert outputs[0].uom == "piece"
    assert outputs[0].qty == pytest.approx(1.0)


def test_run_writes_a_readable_event_log_and_kpis_json(isolated_cwd: Path) -> None:
    before = _run_dir_names(isolated_cwd)

    exit_code = main(["run", str(MODEL_PATH), "--plan", str(PLAN_PATH), "--reps", "1"])

    assert exit_code == 0
    after = _run_dir_names(isolated_cwd)
    added = after - before
    assert len(added) == 1, f"expected exactly one new run directory, found {added}"
    run_dir = isolated_cwd / "runs" / next(iter(added))

    events_path = run_dir / "events.parquet"
    kpis_path = run_dir / "kpis.json"
    assert events_path.is_file()
    assert kpis_path.is_file()

    df = pl.read_parquet(events_path)
    assert df.height > 0
    expected_locations = {
        f"{step}_{size}" for size in SIZES for step in ("cut", "assemble", "burnin", "pack")
    }
    assert set(df["location_id"].unique()) == expected_locations

    kpis = json.loads(kpis_path.read_text(encoding="utf-8"))
    assert "run_hours" in kpis
    assert "setup_hours" in kpis

    # Every finished light the plan demanded, per size variant, actually
    # reached `pack_<size>` -- the conservation invariant that matters, kept
    # robust to the PullRule finding documented in README.md (a wasted
    # zero-qty firing per order contributes 0 to this sum).
    totals_by_part = _plan_total_qty_by_part(PLAN_PATH)
    for size in SIZES:
        part = f"packed_light_{size}"
        pack_rows = df.filter(pl.col("location_id") == f"pack_{size}")
        assert pack_rows["qty"].sum() == pytest.approx(totals_by_part[part])


def test_labor_pool_is_the_constraint_the_machines_have_headroom(isolated_cwd: Path) -> None:
    """The report-level story this example exists to tell: a HIRE decision,
    not a machine-capacity decision. Computed from a REAL in-process run
    (`RunDriver.run` -> `orders_frame` -> `KpiEngine().compute`), the same
    call shape production `SweepHarness._kpi_rows_for_point` uses -- no
    CLI/JSON round-trip, no fixture-baked numbers.

    `labor_pool_capacity` is passed as `assembler_pool`'s own headcount (the
    scarce resource this example is built around) rather than a hardcoded
    magic number, so this stays correct if the model's tuning ever moves."""
    compiled = load_model(str(MODEL_PATH))
    plan = load_plan(str(PLAN_PATH), compiled.registry)
    assembler_headcount = next(
        pool.headcount for pool in compiled.labor_pools if pool.name == "assembler_pool"
    )

    result = RunDriver(compiled).run(plan, seed=1, replication_index=0)
    orders = orders_frame(plan, compiled, result.event_log_path)
    kpis = KpiEngine().compute(
        result.event_log_path,
        orders,
        result.horizon,
        labor_pool_capacity=assembler_headcount,
    )

    labor_utilization = kpis.labor_pool_utilization["default"]

    # (a) the labor pool is saturated -- operators are the constraint.
    assert labor_utilization >= 0.8

    # (b) the labor pool is tighter than EVERY individual machine -- the
    # machines have headroom, so this reads as a people problem, not a
    # machine problem.
    assert kpis.utilization_by_cell
    for location_id, machine_utilization in kpis.utilization_by_cell.items():
        assert labor_utilization > machine_utilization, (
            f"{location_id} utilization {machine_utilization:.3f} is not "
            f"below labor pool utilization {labor_utilization:.3f}"
        )

    # (c) the labor shortage has a real on-time-delivery cost: several
    # orders miss their due date.
    assert kpis.on_time_pct < 100
    late_orders = [
        order_id
        for order_id, lateness in kpis.lateness_by_order.items()
        if lateness is not None and lateness > 0
    ]
    assert len(late_orders) >= 5, f"expected several late orders, found {late_orders}"


def test_registry_declares_every_part_in_the_chain() -> None:
    compiled = load_model(str(MODEL_PATH))
    registry: PartTypeRegistry = compiled.registry

    for size in SIZES:
        for part in (
            f"panel_{size}",
            f"housing_{size}",
            f"light_{size}",
            f"tested_light_{size}",
            f"packed_light_{size}",
        ):
            registry.uom(part)  # raises KeyError if undeclared

    for part in ("diffuser_sheet", "sheet_remainder", "led_strip", "driver"):
        registry.uom(part)
