"""Tier 0 — probabilistic quality gate, end to end through the RunDriver.

A `make` center emits widgets; a quality gate routes each whole widget to `pack`
(pass) or `rework` (fail) by chance, drawing from the dedicated SOURCE_ROUTING
stream so the split is seeded and reproducible (CRN, D-033).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import polars as pl

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_N = 120

_MODEL = textwrap.dedent(
    """
    stocks:
      - {name: raw, uom: piece}
    part_types:
      - {name: raw, uom: piece, attributes: {}}
      - {name: widget, uom: piece, attributes: {}}
      - {name: carton, uom: piece, attributes: {}}
      - {name: reworked, uom: piece, attributes: {}}
    machines:
      - {name: make_m}
      - {name: pack_m}
      - {name: rework_m}
    labor:
      pools:
        - {name: pool, headcount: 3, skills: [op]}
    locations:
      - name: make
        consumes: [{thing: raw, qty: 1, uom: piece}]
        emits: [{thing: widget, qty: 1, uom: piece}]
        setup_key: g
        time_model: {kind: rate_based, rate: 100.0}
        machine: make_m
        labor_skill: op
        quality_gate:
          thing: widget
          branches:
            - {prob: 0.8, to: pack}
            - {prob: 0.2, to: rework}
      - name: pack
        consumes: [{thing: widget, qty: 1, uom: piece}]
        emits: [{thing: carton, qty: 1, uom: piece}]
        setup_key: g
        time_model: {kind: rate_based, rate: 100.0}
        machine: pack_m
        labor_skill: op
      - name: rework
        consumes: [{thing: widget, qty: 1, uom: piece}]
        emits: [{thing: reworked, qty: 1, uom: piece}]
        setup_key: g
        time_model: {kind: rate_based, rate: 100.0}
        machine: rework_m
        labor_skill: op
    routing:
      - {part: carton, steps: [make, pack]}
    processes: []
    bom: []
    """
).strip()


def _counts_by_location(tmp_path: Path, seed: int) -> dict[str, int]:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(_MODEL, encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    rows = ["work_order_id,part,qty,start_date,due_date"]
    rows += [f"wo-{i},carton,1,{i},100000" for i in range(_N)]
    plan_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    compiled = load_model(str(model_path))
    plan = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(plan, seed=seed, replication_index=0)
    events = pl.read_parquet(result.event_log_path)
    grouped = events.group_by("location_id").agg(pl.len().alias("n"))
    return {row["location_id"]: int(row["n"]) for row in grouped.iter_rows(named=True)}


def test_quality_gate_routes_every_widget_down_exactly_one_branch(tmp_path) -> None:
    counts = _counts_by_location(tmp_path, seed=0)
    # Every one of the N widgets is routed to exactly one of pack/rework.
    assert counts.get("pack", 0) + counts.get("rework", 0) == _N


def test_quality_gate_split_follows_declared_probabilities(tmp_path) -> None:
    counts = _counts_by_location(tmp_path, seed=0)
    pack, rework = counts.get("pack", 0), counts.get("rework", 0)
    assert pack > rework  # 0.8 vs 0.2
    assert rework > 0  # the low-probability branch is still exercised
    assert 0.6 <= pack / _N <= 0.95  # loose band around the declared 0.8


def test_quality_gate_split_is_seeded_and_reproducible(tmp_path) -> None:
    first = _counts_by_location(tmp_path, seed=7)
    second = _counts_by_location(tmp_path, seed=7)
    assert first == second  # same seed -> identical routing draws (CRN, D-033)
