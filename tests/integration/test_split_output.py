"""`split_output` — the inverse of `batch_size`. A step that processes a lot as a
group, then explodes its output into individual unit bundles so each unit flows,
queues, holds its own capacity slot, and is tracked to its owning order on its own.

General capability (any operation): divide a dough batch into loaves, cut stock into
blanks, singulate a tray. Validated here on a small two-step model driven through
`RunDriver`, reading the run's own `events.parquet`.
"""

from __future__ import annotations

import polars as pl

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_MODEL = """
stocks: []
part_types:
  - {name: dough, uom: piece, attributes: {}}
  - {name: shaped, uom: piece, attributes: {}}
  - {name: baked, uom: piece, attributes: {}}
machines:
  - {name: bench_m}
  - {name: oven_m}
labor:
  pools:
    - {name: shop, headcount: 1, skills: [op]}
locations:
  - name: shape
    consumes: [{thing: dough, qty: 1, uom: piece}]
    emits: [{thing: shaped, qty: 1, uom: piece}]
    setup_key: g
    split_output: true
    time_model: {kind: rate_based, rate: 0.02}
    machine: bench_m
    labor_skill: op
  - name: bake
    consumes: [{thing: shaped, qty: 1, uom: piece}]
    emits: [{thing: baked, qty: 1, uom: piece}]
    setup_key: g
    capacity: 4
    time_model: {kind: batch_hold, seconds: 3300}
    machine: oven_m
    labor_skill: op
    shift_crossing: finish_unattended
routing:
  - {part: baked, steps: [shape, bake]}
processes: []
bom: []
"""


def _run(tmp_path, qty: int):
    (tmp_path / "model.yaml").write_text(_MODEL, encoding="utf-8")
    (tmp_path / "plan.csv").write_text(
        f"work_order_id,part,qty,start_date,due_date\ntub-1,baked,{qty},0,100000\n",
        encoding="utf-8",
    )
    compiled = load_model(str(tmp_path / "model.yaml"))
    plan = load_plan(str(tmp_path / "plan.csv"), compiled.registry)
    result = RunDriver(compiled).run(plan, seed=0, replication_index=0)
    return pl.read_parquet(result.event_log_path)


def test_split_output_explodes_a_lot_into_individual_units(tmp_path) -> None:
    events = _run(tmp_path, qty=6)

    shape = events.filter(pl.col("location_id") == "shape")
    bake = events.filter(pl.col("location_id") == "bake")

    # shape processes the whole lot of 6 in ONE firing (the group operation)...
    assert shape.height == 1
    assert shape["qty"].to_list() == [6.0]
    # ...but its output is split, so bake sees SIX separate unit jobs, not one lot.
    assert bake.height == 6
    assert bake["qty"].to_list() == [1.0] * 6


def test_split_units_flow_individually_through_a_capacity_gate(tmp_path) -> None:
    """With an oven of 4 slots, 6 split loaves bake as two rounds (4 then 2),
    proving each unit competes for a capacity slot on its own after the split."""
    events = _run(tmp_path, qty=6)
    bake = events.filter(pl.col("location_id") == "bake").sort("actual_start")

    starts = bake["actual_start"].to_list()
    # first four start together (round of 4), the last two start one hold later
    assert starts[:4] == [starts[0]] * 4
    assert starts[4] > starts[0]
    assert starts[4] == starts[5]


def test_split_below_one_leaves_the_bundle_intact(tmp_path) -> None:
    """A lot of exactly one unit has nothing to split: one job in, one job out."""
    events = _run(tmp_path, qty=1)
    assert events.filter(pl.col("location_id") == "bake").height == 1
