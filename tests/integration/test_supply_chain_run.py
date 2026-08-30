"""Integration test: the supply-chain example runs end to end and produces a
per-run inventory log with sane multi-echelon KPIs."""

from __future__ import annotations

from twinflow.instrumentation.inventory import compute_inventory_kpis
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_MODEL = "examples/supply-chain/model.yaml"
_PLAN = "examples/supply-chain/plan.csv"


def test_supply_chain_example_produces_inventory_kpis() -> None:
    compiled = load_model(_MODEL)
    plan = load_plan(_PLAN, compiled.registry)
    result = RunDriver(compiled).run(plan, seed=0, replication_index=0)

    inventory_path = result.event_log_path.parent / "inventory.parquet"
    assert inventory_path.is_file()  # written next to events.parquet, by convention

    kpis = compute_inventory_kpis(inventory_path, result.horizon)
    # The working stock reorders, and its draw cascades an order at the upstream echelon.
    assert kpis.orders_placed["resin"] >= 1
    assert kpis.total_ordered["resin"] > 0.0
    assert kpis.total_ordered["bulk_resin"] > 0.0  # multi-echelon draw propagated
    assert kpis.average_level["resin"] > 0.0
