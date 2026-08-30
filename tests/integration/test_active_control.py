"""Phase A active-control tests: dispatch policies (A1), order-release control (A2),
and disruptions (A4: breakdown, operator absence, rush priority).

Each builds a tiny model in a temp directory and drives it through `RunDriver`, then
reads the run's own `events.parquet` / `run_meta` to assert the control actually changed
behaviour — reproducibly.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from twinflow.model import load_model, validate_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import load_plan

_MODEL = """
defaults: {cycle_time_cv: 0.0}
{TOP}
machines: [{name: m1}]
part_types:
  - {name: raw, uom: piece, attributes: {}}
  - {name: widget, uom: piece, attributes: {}}
stocks: []
labor: {pools: [{name: p, headcount: 1, skills: [op]}]}
locations:
  - name: work
    consumes: [{thing: raw, qty: 1, uom: piece}]
    emits: [{thing: widget, qty: 1, uom: piece}]
    setup_key: g
    {EXTRA}
    time_model: {kind: rate_based, rate: 1.0}
    machine: m1
    labor_skill: op
routing: [{part: widget, steps: [work]}]
"""


def _run(tmp: Path, top: str, extra: str, plan_csv: str, seed: int = 1) -> RunResult:
    yaml_text = _MODEL.replace("{TOP}", top).replace("{EXTRA}", extra)
    (tmp / "m.yaml").write_text(yaml_text)
    (tmp / "p.csv").write_text(plan_csv)
    previous = Path.cwd()
    os.chdir(tmp)
    try:
        model = load_model(str(tmp / "m.yaml"))
        plan = load_plan(str(tmp / "p.csv"), model.registry)
        result = RunDriver(model).run(plan, seed=seed, replication_index=0)
        # RunDriver writes runs/<id>/ relative to the (temp) cwd; resolve to absolute
        # so the event log is readable after we chdir back.
        return replace(result, event_log_path=(tmp / result.event_log_path).resolve())
    finally:
        os.chdir(previous)


def _first_event_qty(event_path: Path) -> float:
    events = pl.read_parquet(event_path).sort("actual_start")
    return float(events["qty"][0])


@pytest.fixture
def workdir(tmp_path: Path) -> Iterator[Path]:
    yield tmp_path


# ------------------------------------------------------------------- A1 dispatch
# Order A (qty 2, due far away) is listed first; order B (qty 8, due soon) second.
# Under FIFO the first firing is A (qty 2); under EDD it is B (qty 8).
_DISPATCH_PLAN = (
    "work_order_id,part,qty,start_date,due_date\n"
    "A,widget,2,0,10000\n"
    "B,widget,8,0,5\n"
)


def test_fifo_runs_arrival_order(workdir: Path) -> None:
    result = _run(workdir, "", "dispatch: fifo", _DISPATCH_PLAN)
    assert _first_event_qty(result.event_log_path) == 2.0  # A first (arrival order)


def test_edd_runs_earliest_due_first(workdir: Path) -> None:
    result = _run(workdir, "", "dispatch: edd", _DISPATCH_PLAN)
    assert _first_event_qty(result.event_log_path) == 8.0  # B first (earliest due)


def test_dispatch_is_reproducible(workdir: Path) -> None:
    a = _run(workdir, "", "dispatch: edd", _DISPATCH_PLAN, seed=7)
    b = _run(workdir, "", "dispatch: edd", _DISPATCH_PLAN, seed=7)
    assert _first_event_qty(a.event_log_path) == _first_event_qty(b.event_log_path)


def test_rush_priority_jumps_the_queue(workdir: Path) -> None:
    # A (qty 2, no priority) listed first; RUSH (qty 8, far due, priority 9) second.
    # Even under FIFO the rush order runs first.
    plan = (
        "work_order_id,part,qty,start_date,due_date,priority\n"
        "A,widget,2,0,5,0\n"
        "RUSH,widget,8,0,99999,9\n"
    )
    result = _run(workdir, "", "dispatch: fifo", plan)
    assert _first_event_qty(result.event_log_path) == 8.0  # the rush order


# --------------------------------------------------------------- A2 order release
_RELEASE_PLAN = "work_order_id,part,qty,start_date,due_date\n" + "".join(
    f"o{i},widget,1,0,1000\n" for i in range(8)
)


def test_wip_cap_completes_all_orders(workdir: Path) -> None:
    result = _run(workdir, "release: {policy: wip_cap, wip_cap: 3}", "", _RELEASE_PLAN)
    events = pl.read_parquet(result.event_log_path)
    assert events.height == 8  # every order ran to completion (no hang, no drop)


def test_wip_cap_one_serializes_flow(workdir: Path) -> None:
    # A stricter cap cannot finish sooner than a looser one on a single machine; with
    # cap 1 the makespan is at least the plan-release makespan.
    capped = _run(workdir, "release: {policy: wip_cap, wip_cap: 1}", "", _RELEASE_PLAN)
    plan = _run(workdir, "", "", _RELEASE_PLAN)
    assert capped.horizon >= plan.horizon - 1e-6


# ------------------------------------------------------------------- A4 breakdown
_BREAKDOWN_PLAN = "work_order_id,part,qty,start_date,due_date\n" + "".join(
    f"o{i},widget,1,0,1000\n" for i in range(25)
)
_BREAKDOWN = "breakdown: {mtbf: 3, mttr: {dist: lognormal, mean: 4, cv: 0.2}}"


def test_breakdown_adds_downtime_and_delays(workdir: Path) -> None:
    healthy = _run(workdir, "", "", _BREAKDOWN_PLAN)
    broken = _run(workdir, "", _BREAKDOWN, _BREAKDOWN_PLAN)
    downtime = broken.run_meta["downtime_seconds_by_machine"]
    assert downtime.get("m1", 0.0) > 0.0
    assert broken.horizon > healthy.horizon


def test_breakdown_is_reproducible(workdir: Path) -> None:
    a = _run(workdir, "", _BREAKDOWN, _BREAKDOWN_PLAN, seed=5)
    b = _run(workdir, "", _BREAKDOWN, _BREAKDOWN_PLAN, seed=5)
    assert a.run_meta["downtime_seconds_by_machine"] == b.run_meta["downtime_seconds_by_machine"]


# ------------------------------------------------------------------- A4 absence
def test_absence_reduces_effective_headcount(workdir: Path) -> None:
    # A pool of 3 with a high absence rate loses slots (seeded), so a floor whose
    # throughput depends on operators slows down. Here we assert the run still
    # completes (floored at 1, never deadlocks) and is reproducible.
    top = ""
    extra = ""
    plan = _RELEASE_PLAN
    yaml_a = _MODEL.replace("{TOP}", top).replace("{EXTRA}", extra).replace(
        "headcount: 1", "headcount: 3, absence_rate: 0.9"
    )
    (workdir / "m.yaml").write_text(yaml_a)
    (workdir / "p.csv").write_text(plan)
    previous = Path.cwd()
    os.chdir(workdir)
    try:
        model = load_model(str(workdir / "m.yaml"))
        wo = load_plan(str(workdir / "p.csv"), model.registry)
        first = RunDriver(model).run(wo, seed=2, replication_index=0)
        second = RunDriver(model).run(wo, seed=2, replication_index=0)
        first_events = pl.read_parquet(workdir / first.event_log_path).height
    finally:
        os.chdir(previous)
    assert first.horizon == second.horizon  # seeded absence is reproducible
    assert first_events == 8


# ---------------------------------------------------------------------- validation
def test_validate_rejects_unknown_dispatch(workdir: Path) -> None:
    yaml_text = _MODEL.replace("{TOP}", "").replace("{EXTRA}", "dispatch: sooner")
    (workdir / "m.yaml").write_text(yaml_text)
    errors = validate_model(str(workdir / "m.yaml"))
    assert any("dispatch" in e.path for e in errors)


def test_validate_rejects_capped_release_without_cap(workdir: Path) -> None:
    yaml_text = _MODEL.replace("{TOP}", "release: {policy: conwip}").replace("{EXTRA}", "")
    (workdir / "m.yaml").write_text(yaml_text)
    errors = validate_model(str(workdir / "m.yaml"))
    assert any("wip_cap" in e.path for e in errors)
