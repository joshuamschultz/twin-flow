from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from twinflow.instrumentation.aggregate import aggregate_kpis
from twinflow.instrumentation.event_log import EVENT_LOG_SCHEMA
from twinflow.instrumentation.kpis import KpiEngine, KpiSet
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan


def _empty_kpis(completion: float | None) -> KpiSet:
    return KpiSet(
        completion_by_order={"o": completion},
        lateness_by_order={"o": completion},
        on_time_pct=0.0,
        utilization_by_cell={},
        utilization_by_machine={},
        wait_seconds_by_location={},
        labor_pool_utilization={},
        wip_by_location=pl.DataFrame(),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=0.0,
        setup_hours=0.0,
        event_counts_by_location={},
    )


def test_censored_completion_is_not_estimated_from_completed_subset() -> None:
    aggregated = aggregate_kpis(
        [_empty_kpis(10.0), _empty_kpis(None), _empty_kpis(30.0)], level=0.95
    )
    distribution = aggregated.completion_distributions["o"]
    assert distribution.observed_count == 2
    assert distribution.censored_count == 1
    assert distribution.quantiles is None
    assert distribution.estimability == "not_estimable"


def test_terminal_quantity_ledger_requires_full_accepted_quantity(tmp_path: Path) -> None:
    events = pl.DataFrame(
        [("cut", "raw", "lot-1", "transform", 10.0, 0.0, None, 0.0, 5.0, 5.0, "complete", 0.0)],
        schema=EVENT_LOG_SCHEMA,
        orient="row",
    )
    path = tmp_path / "events.parquet"
    events.write_parquet(path)
    orders = pl.DataFrame(
        [("o", "finished", 20.0, "", 10.0, 4.0, 6.0, None)],
        schema={
            "order_id": pl.Utf8,
            "part_id": pl.Utf8,
            "due_date": pl.Float64,
            "lot_id": pl.Utf8,
            "required_qty": pl.Float64,
            "accepted_qty": pl.Float64,
            "scrapped_qty": pl.Float64,
            "completion_time": pl.Float64,
        },
        orient="row",
    )
    result = KpiEngine().compute(path, orders, horizon=10.0)
    outcome = result.order_outcomes["o"]
    assert outcome.status == "incomplete_at_horizon"
    assert outcome.accepted_qty == 4.0
    assert outcome.scrapped_qty == 6.0
    assert outcome.remaining_qty == 6.0
    assert result.completion_by_order["o"] is None


def test_bounded_run_retains_evidence_without_changing_cwd(tmp_path: Path) -> None:
    model_path = "examples/cnc-shop/model.yaml"
    compiled = load_model(model_path)
    plan = load_plan("examples/cnc-shop/plan.csv", compiled.registry)
    before = Path.cwd()
    result = RunDriver(compiled).run(
        plan,
        seed=7,
        replication_index=0,
        artifact_dir=tmp_path,
        max_sim_time=0.0,
    )
    assert Path.cwd() == before
    assert result.termination_reason == "sim_time_limit"
    assert result.outcome == "incomplete_at_horizon"
    assert all(item["remaining_qty"] > 0 for item in result.order_outcomes.values())
    assert result.artifact_dir is not None
    evidence = json.loads((result.artifact_dir / "run_meta.json").read_text(encoding="utf-8"))
    assert evidence["limits"]["max_sim_time"] == 0.0
    assert evidence["orders"]
