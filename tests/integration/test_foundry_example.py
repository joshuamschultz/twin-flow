"""FOUNDRY worked example — integration test.

`examples/foundry/model.yaml` + `examples/foundry/plan.csv` is a sand-casting
floor expressed purely as config, no Python: two parallel casting lines share
one raw metal feedstock and one recycle stock.

  Line A: melt_a -> pour_a -> clean_a -> machine_a  =>  bracket
  Line B: melt_b -> pour_b -> clean_b -> machine_b  =>  flywheel

`clean_a`/`clean_b` each scrap a fraction of the castings they clean; that
scrap is routed back to a shared, named top-level stock (`output_stocks:
{remelt: remelt}`) instead of a dead-end sink -- the general "route an
output back to a Stock" recycle-loop capability (D-044), exercised here the
same way `tests/unit/test_stock_destination.py` proves it at the unit layer.

This file exercises the example exactly as a client would run it: `load_model`
-> `validate_model` -> `twinflow.cli.main.main(["run", ...])` (the same path
`twinflow run` takes on the command line) -> a readable event log and
`kpis.json` -- then separately drives `plan.driver.RunDriver` directly (the
only place `RunResult.run_meta["stock_levels"]` is exposed) to prove the
remelt stock actually gains the scrapped material.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import polars as pl
import pytest

from twinflow.cli.main import main
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "foundry"
MODEL_PATH = EXAMPLE_DIR / "model.yaml"
PLAN_PATH = EXAMPLE_DIR / "plan.csv"


def _plan_orders_and_totals(path: Path) -> tuple[int, dict[str, float]]:
    """(number of work orders, total demanded qty per part) read straight from
    the plan CSV, so assertions track the plan instead of hard-coded counts."""
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    totals: dict[str, float] = {}
    for row in rows:
        totals[row["part"]] = totals.get(row["part"], 0.0) + float(row["qty"])
    return len(rows), totals


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


def test_model_loads_and_routes_both_finished_parts() -> None:
    compiled = load_model(str(MODEL_PATH))

    assert compiled.routing["bracket"] == ["melt_a", "pour_a", "clean_a", "machine_a"]
    assert compiled.routing["flywheel"] == ["melt_b", "pour_b", "clean_b", "machine_b"]
    # Both lines' BOM rolls up to raw `metal_ingot` only -- every intermediate
    # part (molten_*, *_casting, *_clean) resolves away.
    assert set(compiled.bom["bracket"]) == {"metal_ingot"}
    assert set(compiled.bom["flywheel"]) == {"metal_ingot"}


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
    assert set(df["location_id"].unique()) == {
        "melt_a",
        "pour_a",
        "clean_a",
        "machine_a",
        "melt_b",
        "pour_b",
        "clean_b",
        "machine_b",
    }

    kpis = json.loads(kpis_path.read_text(encoding="utf-8"))
    assert "run_hours" in kpis
    assert "setup_hours" in kpis

    # Every finished bracket/flywheel the plan demanded actually reached its
    # line's final machining step.
    n_orders, totals_by_part = _plan_orders_and_totals(PLAN_PATH)
    assert n_orders >= 8, "order book should be a realistic 8-10 work orders"
    assert set(totals_by_part) == {"bracket", "flywheel"}, "plan should mix part types"

    bracket_rows = df.filter(pl.col("location_id") == "machine_a")
    flywheel_rows = df.filter(pl.col("location_id") == "machine_b")
    assert bracket_rows["qty"].sum() == pytest.approx(totals_by_part["bracket"])
    assert flywheel_rows["qty"].sum() == pytest.approx(totals_by_part["flywheel"])


def test_remelt_stock_level_rises_from_the_scrap_routed_back_by_both_lines(
    isolated_cwd: Path,
) -> None:
    """The remelt loop: `clean_a`/`clean_b`'s scrap output routes to the
    shared `remelt` stock via `output_stocks`, read back through
    `RunResult.run_meta["stock_levels"]` -- the same seam
    `tests/unit/test_stock_destination.py` uses to prove the wiring."""
    compiled = load_model(str(MODEL_PATH))
    plan = load_plan(str(PLAN_PATH), compiled.registry)
    driver = RunDriver(compiled)

    result = driver.run(plan, seed=1, replication_index=0)

    stock_levels = result.run_meta.get("stock_levels")
    assert stock_levels is not None, (
        "RunResult.run_meta['stock_levels'] is missing -- the remelt loop is not wired"
    )
    assert "remelt" in stock_levels
    assert stock_levels["remelt"] > 0.0, (
        "the remelt stock must have gained scrapped material from clean_a/clean_b, "
        f"got {stock_levels['remelt']!r}"
    )
