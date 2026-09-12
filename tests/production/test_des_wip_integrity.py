"""Active-WIP snapshot regressions for the legacy DES run boundary."""

from __future__ import annotations

import math
from pathlib import Path

import polars as pl
import pytest

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder


def _model() -> str:
    return """
defaults: {cycle_time_cv: 0.0}
stocks:
  - {name: lubricant, initial: 10, thing: lube, uom: lb}
part_types:
  - {name: raw, uom: piece, attributes: {}}
  - {name: middle, uom: piece, attributes: {}}
  - {name: done, uom: piece, attributes: {}}
  - {name: lube, uom: lb, attributes: {}}
machines: [{name: m1}, {name: m2}]
labor: {pools: [{name: floor, skills: [operator], headcount: 2}]}
processes: []
bom: []
locations:
  - name: first
    consumes: [{thing: raw, qty: 1, uom: piece}]
    emits: [{thing: middle, qty: 1, uom: piece}]
    setup_key: first-family
    changeover_seconds: 11
    time_model: {kind: rate_based, rate: 0.02}
    machine: m1
    labor_skill: operator
    material: {stock: lubricant, qty: 2, uom: lb}
  - name: second
    consumes: [{thing: middle, qty: 1, uom: piece}]
    emits: [{thing: done, qty: 1, uom: piece}]
    setup_key: second-family
    time_model: {kind: rate_based, rate: 0.05}
    machine: m2
    labor_skill: operator
routing:
  - {part: done, steps: [first, second]}
"""


def _driver(tmp_path: Path) -> RunDriver:
    path = tmp_path / "model.yaml"
    path.write_text(_model(), encoding="utf-8")
    return RunDriver(load_model(str(path)))


def test_active_wip_skips_consumed_setup_and_material_then_runs_downstream_normally(
    tmp_path: Path,
) -> None:
    result = _driver(tmp_path).run(
        [
            WorkOrder(
                "WO-WIP",
                "done",
                1,
                "100",
                "1000",
                initial_wip_location="first",
                initial_wip_qty=1,
                initial_wip_remaining_time=7,
            )
        ],
        seed=1,
        replication_index=0,
        artifact_dir=tmp_path / "runs",
    )
    events = pl.read_parquet(result.event_log_path).sort("actual_start")
    assert events["location_id"].to_list() == ["first", "second"]
    durations = events["actual_end"] - events["actual_start"]
    assert durations.to_list() == pytest.approx([7, 20])
    assert events.row(0, named=True)["setup_seconds"] == 0
    assert result.run_meta["stock_levels"] == {"lubricant": 10.0}
    assert result.order_outcomes["WO-WIP"]["status"] == "completed"


def test_completed_snapshot_and_early_wip_are_registered_before_delayed_release(
    tmp_path: Path,
) -> None:
    driver = _driver(tmp_path)
    wip = driver.run(
        [
            WorkOrder(
                "WO-EARLY",
                "done",
                1,
                "100",
                "1000",
                initial_wip_location="second",
                initial_wip_qty=1,
                initial_wip_remaining_time=5,
            )
        ],
        seed=1,
        replication_index=0,
        artifact_dir=tmp_path / "early",
    )
    assert wip.order_outcomes["WO-EARLY"]["status"] == "completed"
    completed = driver.run(
        [WorkOrder("WO-DONE", "done", 1, "100", "1000", completed_good_qty=1)],
        seed=1,
        replication_index=1,
        artifact_dir=tmp_path / "done",
    )
    assert pl.read_parquet(completed.event_log_path).is_empty()
    assert completed.order_outcomes["WO-DONE"]["completion_time"] == 0


def test_work_order_snapshot_validation_and_positional_priority_compatibility() -> None:
    positional = WorkOrder("WO", "done", 1, "0", "1", None, None, None, 7)
    assert positional.priority == 7
    assert positional.completed_good_qty == 0
    WorkOrder("LEGACY", "done", 0, "0", "1", "first", 1, 0)
    with pytest.raises(ValueError):
        WorkOrder("BAD", "done", 10, "0", "1", "first", 6, 1, completed_good_qty=5)
    with pytest.raises(ValueError):
        WorkOrder("BAD", "done", 1, "0", "1", "first", 1, math.inf)
    with pytest.raises(ValueError):
        WorkOrder("BAD", "done", 1, "0", "1", completed_good_qty=-1)
