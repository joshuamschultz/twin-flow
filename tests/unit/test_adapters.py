"""Unit tests for twinflow.adapters — the external-integration surfaces.

Covers connectors (field mapping + CSV plan source), demand generation, forecasting,
plan-vs-actual reconciliation, fulfillment KPIs, and the dispatch-name discovery and
validation contract (`twinflow.policy.DISPATCH_NAMES`).
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest
import yaml

from twinflow.adapters import (
    CsvPlanSource,
    FieldMapping,
    FixedDemand,
    JsonResultSink,
    MovingAverageForecaster,
    NaiveForecaster,
    ParquetActualSource,
    PoissonDemand,
    compute_fulfillment,
    reconcile,
)
from twinflow.adapters.demand import DEMAND_GENERATORS
from twinflow.adapters.forecast import FORECASTERS
from twinflow.instrumentation.kpis import KpiSet
from twinflow.model import load_model, validate_model
from twinflow.policy import DISPATCH_NAMES


def _kpis(
    on_time: float,
    run_hours: float = 10.0,
    completion: dict[str, float | None] | None = None,
    lateness: dict[str, float | None] | None = None,
    utilization: dict[str, float] | None = None,
) -> KpiSet:
    return KpiSet(
        completion_by_order=completion if completion is not None else {"wo-1": 100.0},
        lateness_by_order=lateness if lateness is not None else {"wo-1": -50.0},
        on_time_pct=on_time,
        utilization_by_cell=utilization if utilization is not None else {"cut": 0.5},
        utilization_by_machine={"cut": 0.5},
        wait_seconds_by_location={},
        labor_pool_utilization={"default": 0.5},
        wip_by_location=pl.DataFrame(),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=run_hours,
        setup_hours=0.0,
        event_counts_by_location={},
    )


# ------------------------------------------------------------------- connectors
def test_field_mapping_renames_header() -> None:
    mapping = FieldMapping({"work_order_id": "WO", "part": "Item"})
    assert mapping.apply(["WO", "Item", "qty"]) == ["work_order_id", "part", "qty"]


def test_csv_plan_source_maps_erp_columns(tmp_path: Path) -> None:
    registry = load_model("examples/cnc-shop/model.yaml").registry
    csv_path = tmp_path / "erp_plan.csv"
    csv_path.write_text("WO,Item,Quantity,Release,Due\nSO-1,shaft,10,0,1000\n", encoding="utf-8")
    source = CsvPlanSource(
        str(csv_path),
        FieldMapping(
            {
                "work_order_id": "WO",
                "part": "Item",
                "qty": "Quantity",
                "start_date": "Release",
                "due_date": "Due",
            }
        ),
    )
    orders = source.read_plan(registry)
    assert len(orders) == 1
    assert orders[0].part == "shaft" and orders[0].qty == 10


def test_json_result_sink_writes(tmp_path: Path) -> None:
    out = tmp_path / "result.json"
    JsonResultSink(str(out)).write_results({"on_time_pct": 88.0})
    assert json.loads(out.read_text())["on_time_pct"] == 88.0


def test_parquet_actual_source_reads(tmp_path: Path) -> None:
    path = tmp_path / "actual.parquet"
    pl.DataFrame({"location_id": ["cut"], "actual_start": [0.0]}).write_parquet(path)
    assert ParquetActualSource(str(path)).read_actuals().shape == (1, 2)


# ----------------------------------------------------------------------- demand
def test_fixed_demand_generates_spaced_orders() -> None:
    orders = FixedDemand(parts=("a", "b"), count=4, qty=5, horizon=400, lead_time=100).generate()
    assert len(orders) == 4
    assert [o.part for o in orders] == ["a", "b", "a", "b"]
    assert all(o.qty == 5 for o in orders)
    assert orders[1].start_date == "100"


def test_poisson_demand_is_reproducible() -> None:
    gen = PoissonDemand(parts=("a",), rate=0.01, horizon=1000, lead_time=100)
    first = gen.generate(seed=7)
    second = gen.generate(seed=7)
    assert [o.work_order_id for o in first] == [o.work_order_id for o in second]
    assert [o.qty for o in first] == [o.qty for o in second]


def test_demand_generators_registered() -> None:
    assert set(DEMAND_GENERATORS.names()) == {"fixed", "poisson"}


# --------------------------------------------------------------------- forecast
def test_naive_forecaster_repeats_last() -> None:
    model = NaiveForecaster()
    model.fit([1.0, 2.0, 5.0])
    assert model.predict(3) == [5.0, 5.0, 5.0]


def test_moving_average_forecaster() -> None:
    model = MovingAverageForecaster(window=2)
    model.fit([1.0, 3.0, 5.0])
    assert model.predict(2) == [4.0, 4.0]


def test_forecaster_before_fit_raises() -> None:
    with pytest.raises(RuntimeError):
        NaiveForecaster().predict(1)
    with pytest.raises(ValueError):
        NaiveForecaster().fit([])


def test_forecasters_registered() -> None:
    assert set(FORECASTERS.names()) == {"naive", "moving_average"}


# -------------------------------------------------------------------- reconcile
def test_reconcile_diffs_plan_and_actual() -> None:
    planned = _kpis(80.0, run_hours=10.0, lateness={"a": -100.0}, utilization={"cut": 0.5})
    actual = _kpis(70.0, run_hours=12.0, lateness={"a": 200.0}, utilization={"cut": 0.6})
    variance = reconcile(planned, actual)
    assert variance.on_time_delta == pytest.approx(-10.0)
    assert variance.run_hours_delta == pytest.approx(2.0)
    assert variance.utilization_delta_by_cell["cut"] == pytest.approx(0.1)
    assert variance.lateness_delta_by_order["a"] == pytest.approx(300.0)


# ------------------------------------------------------------------ fulfillment
def test_fulfillment_counts_deliveries_and_backorders() -> None:
    kpis = _kpis(
        50.0,
        completion={"a": 100.0, "b": None, "c": 200.0},
        lateness={"a": -50.0, "b": None, "c": 300.0},
    )
    f = compute_fulfillment(kpis)
    assert f.orders == 3
    assert f.delivered == 2 and f.backordered == 1
    assert f.fill_rate == pytest.approx(2 / 3)
    assert f.on_time_delivery_pct == pytest.approx(50.0)  # a on time, c late
    assert f.avg_delivery_lateness == pytest.approx(125.0)


# ------------------------------------------------------------ dispatch names
def _model_with_dispatch(tmp_path: Path, dispatch: str) -> str:
    raw = yaml.safe_load(Path("examples/cnc-shop/model.yaml").read_text(encoding="utf-8"))
    raw["locations"][0]["dispatch"] = dispatch
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return str(path)


def test_dispatch_names_is_the_single_public_source() -> None:
    assert list(DISPATCH_NAMES) == ["fifo", "edd", "spt", "critical_ratio"]


def test_api_adapters_lists_dispatch_names(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from twinflow.service.app import create_app

    payload = (
        TestClient(create_app("examples", workspace_root=tmp_path)).get("/api/adapters").json()
    )
    assert payload["dispatch_policies"] == list(DISPATCH_NAMES)


@pytest.mark.parametrize("name", DISPATCH_NAMES)
def test_validate_accepts_every_dispatch_name(tmp_path: Path, name: str) -> None:
    errors = validate_model(_model_with_dispatch(tmp_path, name))
    assert not [e for e in errors if e.path.endswith(".dispatch")]


def test_validate_rejects_dispatch_name_outside_dispatch_names(tmp_path: Path) -> None:
    assert "sooner" not in DISPATCH_NAMES
    errors = validate_model(_model_with_dispatch(tmp_path, "sooner"))
    assert [e for e in errors if e.path == "locations[0].dispatch"]
