"""DES regressions for global machine identity and attributed active WIP (TF-WS-004/005)."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder


def _model(
    machine_b: str = "441",
    *,
    setup_key_b: str = "family-b",
    changeover_seconds: float = 0,
) -> str:
    return f"""
defaults: {{cycle_time_cv: 0.0}}
stocks: []
part_types:
  - {{name: raw-a, uom: piece, attributes: {{}}}}
  - {{name: done-a, uom: piece, attributes: {{}}}}
  - {{name: raw-b, uom: piece, attributes: {{}}}}
  - {{name: done-b, uom: piece, attributes: {{}}}}
machines: [{{name: "441"}}, {{name: "343"}}]
labor: {{pools: [{{name: floor, skills: [operator], headcount: 2}}]}}
processes: []
bom: []
locations:
  - name: recipe-a
    consumes: [{{thing: raw-a, qty: 1, uom: piece}}]
    emits: [{{thing: done-a, qty: 1, uom: piece}}]
    setup_key: family-a
    changeover_seconds: {changeover_seconds}
    time_model: {{kind: rate_based, rate: 0.016666666666666666}}
    machine: "441"
    labor_skill: operator
  - name: recipe-b
    consumes: [{{thing: raw-b, qty: 1, uom: piece}}]
    emits: [{{thing: done-b, qty: 1, uom: piece}}]
    setup_key: {setup_key_b}
    changeover_seconds: {changeover_seconds}
    time_model: {{kind: rate_based, rate: 0.016666666666666666}}
    machine: "{machine_b}"
    labor_skill: operator
routing:
  - {{part: done-a, steps: [recipe-a]}}
  - {{part: done-b, steps: [recipe-b]}}
"""


def _run(tmp_path: Path, model_text: str, *, replication_index: int = 0):
    model_path = tmp_path / f"model-{replication_index}.yaml"
    model_path.write_text(model_text, encoding="utf-8")
    compiled = load_model(str(model_path))
    plan = [
        WorkOrder("WO-A", "done-a", 1, "0", "1000"),
        WorkOrder("WO-B", "done-b", 1, "0", "1000"),
    ]
    return RunDriver(compiled).run(
        plan,
        seed=7,
        replication_index=replication_index,
        artifact_dir=tmp_path / "runs",
    )


def _intervals(result) -> list[tuple[float, float]]:
    frame = pl.read_parquet(result.event_log_path).sort("actual_start")
    return list(zip(frame["actual_start"].to_list(), frame["actual_end"].to_list(), strict=True))


def test_two_recipes_referencing_one_machine_serialize_in_direct_run_driver(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _model("441"))
    intervals = _intervals(result)
    assert len(intervals) == 2
    assert intervals[0][1] <= intervals[1][0]
    resource_rows = pl.read_parquet(result.resource_usage_path)
    assert set(resource_rows["machine_id"].to_list()) == {"441"}
    assert result.horizon >= 120


def test_distinct_physical_machines_overlap_and_replications_are_independent(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "independent-model.yaml"
    model_path.write_text(_model("343"), encoding="utf-8")
    compiled = load_model(str(model_path))
    plan = [
        WorkOrder("WO-A", "done-a", 1, "0", "1000"),
        WorkOrder("WO-B", "done-b", 1, "0", "1000"),
    ]
    driver = RunDriver(compiled)
    first = driver.run(
        plan,
        seed=7,
        replication_index=0,
        artifact_dir=tmp_path / "runs",
    )
    second = driver.run(
        plan,
        seed=7,
        replication_index=1,
        artifact_dir=tmp_path / "runs",
    )
    first_intervals = _intervals(first)
    second_intervals = _intervals(second)
    assert first_intervals == second_intervals
    assert first_intervals[0][0] < first_intervals[1][1]
    assert first_intervals[1][0] < first_intervals[0][1]


def test_same_physical_machine_shares_setup_state_across_recipe_locations(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        _model("441", setup_key_b="family-a", changeover_seconds=10),
    )
    resources = pl.read_parquet(result.resource_usage_path).sort("setup_start")
    assert resources["machine_id"].to_list() == ["441", "441"]
    assert resources["setup_seconds"].sum() == pytest.approx(10)


def test_active_wip_resumes_remaining_time_with_order_identity_and_no_raw_rerelease(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "wip-model.yaml"
    model_path.write_text(_model("343"), encoding="utf-8")
    compiled = load_model(str(model_path))
    order = WorkOrder(
        "WO-WIP",
        "done-a",
        100,
        "0",
        "1000",
        initial_wip_location="recipe-a",
        initial_wip_qty=40,
        initial_wip_remaining_time=10,
        completed_good_qty=60,
    )
    result = RunDriver(compiled).run(
        [order],
        seed=1,
        replication_index=0,
        artifact_dir=tmp_path / "runs",
    )
    events = pl.read_parquet(result.event_log_path)

    assert events.height == 1
    event = events.row(0, named=True)
    assert event["actual_end"] - event["actual_start"] == pytest.approx(10)
    assert event["qty"] == 40
    assert event["lot_id"] == "WO-WIP"
    outcome = result.order_outcomes["WO-WIP"]
    assert outcome["produced_qty"] == 100
    assert outcome["quality_accepted_qty"] == 100
    assert outcome["shipped_qty"] == 0
    assert outcome["remaining_qty"] == 0
