from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from twinflow.instrumentation.kpis import KpiEngine
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder


def test_capacity_two_has_independent_cold_setups_and_resource_ids(tmp_path: Path) -> None:
    compiled = load_model("examples/hmlv-calendar/model.yaml")
    plan = [
        WorkOrder("A", "finished", 1, "0", "100000"),
        WorkOrder("B", "finished", 1, "0", "100000"),
        WorkOrder("C", "finished", 1, "0", "100000"),
    ]
    result = RunDriver(compiled).run(plan, 3, 0, artifact_dir=tmp_path)
    assert result.resource_usage_path is not None
    usage = pl.read_parquet(result.resource_usage_path)
    assert set(usage["machine_id"]) == {"mill-1", "mill-2"}
    assert sorted(usage["setup_seconds"].to_list()) == pytest.approx([0.0, 600.0, 600.0])
    first_two = usage.sort("run_start").head(2)
    assert first_two["run_start"].to_list() == pytest.approx([600.0, 600.0])
    assert first_two["run_end"].to_list() == pytest.approx([4200.0, 4200.0])

    kpis = KpiEngine().compute(
        result.event_log_path,
        orders_frame(plan, compiled, result.event_log_path),
        result.horizon,
    )
    assert set(kpis.machine_hours_by_machine) == {"mill-1", "mill-2"}
    assert kpis.utilization_by_cell["mill"] == pytest.approx(12_000.0 / 15_600.0)


def test_invalid_calendar_and_crossing_fail_validation(tmp_path: Path) -> None:
    source = Path("examples/hmlv-calendar/model.yaml").read_text(encoding="utf-8")
    invalid = source.replace("America/Chicago", "Mars/Olympus").replace(
        "shift_crossing: pause", "shift_crossing: teleport"
    )
    path = tmp_path / "invalid.yaml"
    path.write_text(invalid, encoding="utf-8")
    errors = validate_model(str(path))
    paths = {error.path for error in errors}
    assert "labor.pools[0].calendar.timezone" in paths
    assert "locations[0].shift_crossing" in paths
