"""The `cnc-shop-3mill` worked example (capacity-N, COMP-030): the CNC mill step
runs three parallel machines. Asserts the example validates, compiles with the
declared capacity, runs to a terminating result, and — the point of the example
— clears the milled work faster than the single-mill base shop.
"""

from __future__ import annotations

from pathlib import Path

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
_3MILL = _EXAMPLES / "cnc-shop-3mill"
_BASE = _EXAMPLES / "cnc-shop"


def _mill_makespan(example: Path) -> float:
    compiled = load_model(str(example / "model.yaml"))
    work_orders = load_plan(str(example / "plan.csv"), compiled.registry)
    result = RunDriver(compiled).run(work_orders, seed=0, replication_index=0)
    orders = orders_frame(work_orders, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    return max(v for v in kpis.completion_by_order.values() if v is not None)


def test_example_validates() -> None:
    assert validate_model(str(_3MILL / "model.yaml")) == []


def test_mill_centers_compile_with_capacity_three() -> None:
    compiled = load_model(str(_3MILL / "model.yaml"))
    by_id = {loc.location_id: loc for loc in compiled.locations}
    assert by_id["mill_shaft"].capacity == 3
    assert by_id["mill_bracket"].capacity == 3


def test_three_mills_finish_sooner_than_the_base_shop() -> None:
    assert _mill_makespan(_3MILL) < _mill_makespan(_BASE)
