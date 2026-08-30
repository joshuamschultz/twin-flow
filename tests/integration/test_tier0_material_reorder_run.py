"""Tier 0 — a location consuming a finite, self-refilling material stock.

`make` pulls `glue` (a secondary `material` requirement) from a reorder-point
stock that starts EMPTY. Without replenishment the run would deadlock on the very
first firing; the reorder policy tops the stock up so the plan completes.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import polars as pl

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_N = 10


def _model(reorder: bool) -> str:
    glue = (
        "  - {name: glue, uom: ml, initial: 0, reorder_point: 20, refill_to: 200}"
        if reorder
        else "  - {name: glue, uom: ml, initial: 100000}"
    )
    return textwrap.dedent(
        f"""
        stocks:
          - {{name: raw, uom: piece}}
        {glue}
        part_types:
          - {{name: raw, uom: piece, attributes: {{}}}}
          - {{name: widget, uom: piece, attributes: {{}}}}
        machines:
          - {{name: make_m}}
        labor:
          pools:
            - {{name: pool, headcount: 1, skills: [op]}}
        locations:
          - name: make
            consumes: [{{thing: raw, qty: 1, uom: piece}}]
            emits: [{{thing: widget, qty: 1, uom: piece}}]
            setup_key: g
            material: {{stock: glue, qty: 5, uom: ml}}
            time_model: {{kind: rate_based, rate: 100.0}}
            machine: make_m
            labor_skill: op
        routing:
          - {{part: widget, steps: [make]}}
        processes: []
        bom: []
        """
    ).strip()


def _run(tmp_path: Path, reorder: bool) -> tuple[int, float]:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(_model(reorder), encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    rows = ["work_order_id,part,qty,start_date,due_date"]
    rows += [f"wo-{i},widget,1,{i},100000" for i in range(_N)]
    plan_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    compiled = load_model(str(model_path))
    plan = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(plan, seed=0, replication_index=0)
    events = pl.read_parquet(result.event_log_path)
    made = int(events.filter(pl.col("location_id") == "make").height)
    glue_level = float(result.run_meta["stock_levels"]["glue"])
    return made, glue_level


def test_finite_reorder_material_stock_lets_the_plan_complete(tmp_path) -> None:
    made, glue_level = _run(tmp_path, reorder=True)
    # All N firings ran despite the glue stock starting empty: the reorder policy
    # replenished it before each blocking pull.
    assert made == _N
    assert glue_level >= 0.0


def test_reorder_matches_an_effectively_infinite_stock(tmp_path) -> None:
    made_reorder, _ = _run(tmp_path, reorder=True)
    made_infinite, _ = _run(tmp_path, reorder=False)
    # The finished-work outcome is identical; reorder only changes how the stock is
    # kept fed, not what the floor produces.
    assert made_reorder == made_infinite == _N
