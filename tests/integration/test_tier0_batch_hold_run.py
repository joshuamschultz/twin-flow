"""Tier 0 — batch/hold operation end to end (accumulate, then one group hold).

An `oven` with `batch_size: "5 piece"` and a `batch_hold` time model accumulates
five arriving units into one firing and charges ONE hold duration for the whole
group, not a per-unit time. Ten units therefore fire the oven twice.
"""

from __future__ import annotations

import textwrap

import polars as pl

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_HOLD = 300.0
_BATCH = 5
_N = 10

_MODEL = textwrap.dedent(
    f"""
    stocks:
      - {{name: raw, uom: piece}}
    part_types:
      - {{name: raw, uom: piece, attributes: {{}}}}
      - {{name: baked, uom: piece, attributes: {{}}}}
    machines:
      - {{name: oven_m}}
    labor:
      pools:
        - {{name: pool, headcount: 1, skills: [op]}}
    locations:
      - name: oven
        consumes: [{{thing: raw, qty: 1, uom: piece}}]
        emits: [{{thing: baked, qty: 1, uom: piece}}]
        setup_key: g
        batch_size: "{_BATCH} piece"
        time_model: {{kind: batch_hold, seconds: {_HOLD}}}
        machine: oven_m
        labor_skill: op
    routing:
      - {{part: baked, steps: [oven]}}
    processes: []
    bom: []
    """
).strip()


def test_batch_hold_fires_once_per_group_and_holds_the_whole_group_at_once(tmp_path) -> None:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(_MODEL, encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    rows = ["work_order_id,part,qty,start_date,due_date"]
    rows += [f"wo-{i},baked,1,0,100000" for i in range(_N)]
    plan_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    compiled = load_model(str(model_path))
    plan = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(plan, seed=0, replication_index=0)
    events = pl.read_parquet(result.event_log_path).filter(pl.col("location_id") == "oven")

    # Ten units, batches of five -> exactly two oven firings.
    assert events.height == _N // _BATCH
    # Each firing holds the whole group for one hold duration, independent of qty.
    durations = (events["actual_end"] - events["actual_start"]).to_list()
    assert all(abs(d - _HOLD) < 1e-9 for d in durations)
    # Each firing processed a full batch of five together.
    assert events["qty"].to_list() == [float(_BATCH), float(_BATCH)]
