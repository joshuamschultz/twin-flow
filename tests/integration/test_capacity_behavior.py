"""Capacity-N behaviour (COMP-030): N parallel machines at one center raise its
throughput, so a capacity-3 bottleneck clears strictly more work than the same
center at capacity 1 in the same horizon. The engine already pulls jobs from a
`machine_pool`; capacity-N sizes that pool from config.
"""

from __future__ import annotations

from pathlib import Path

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_MODEL = """
defaults: {{cycle_time_cv: 0.0}}
stocks: []
part_types:
  - {{name: raw, uom: piece, attributes: {{}}}}
  - {{name: done, uom: piece, attributes: {{}}}}
machines: [{{name: m}}]
labor: {{pools: [{{name: p, skills: [s], headcount: 3}}]}}
processes: []
bom: []
locations:
  - name: mill
    consumes: [{{thing: raw, qty: 1, uom: piece}}]
    emits: [{{thing: done, qty: 1, uom: piece}}]
    setup_key: g
    capacity: {capacity}
    time_model: {{kind: rate_based, rate: 0.05}}
    machine: m
    labor_skill: s
routing:
  - part: done
    steps: [mill]
"""

_PLAN = "work_order_id,part,qty,start_date,due_date\n" + "".join(
    f"wo-{i},done,1,0,100\n" for i in range(1, 13)
)


def _makespan(tmp_path: Path, capacity: int) -> float:
    """Time the last order finishes: three parallel machines clear the backlog
    sooner than one."""
    model_path = tmp_path / f"m{capacity}.yaml"
    model_path.write_text(_MODEL.format(capacity=capacity), encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    plan_path.write_text(_PLAN, encoding="utf-8")

    compiled = load_model(str(model_path))
    work_orders = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(work_orders, seed=0, replication_index=0)
    orders = orders_frame(work_orders, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    completions = [v for v in kpis.completion_by_order.values() if v is not None]
    return max(completions)


def test_three_machines_clear_the_backlog_sooner(tmp_path: Path) -> None:
    one = _makespan(tmp_path, capacity=1)
    three = _makespan(tmp_path, capacity=3)
    # Three parallel machines finish the same 12 jobs in roughly a third the time.
    assert three < one * 0.6
