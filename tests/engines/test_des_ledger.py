"""T-247: the DES ledger (COMP-153, REQ-198, D-141).

Hand-checked on a two-location floor that shares one press:

    t=0    order A (widget-a, due 50 s) starts on the press: setup None -> grp-a,
           glue 10 -> 8 kg.  Order B (widget-b, due 1000 s) waits.
    t=100  A finishes (50 s late). B starts: setup grp-a -> grp-b, glue 8 -> 6 kg.
    t=200  B finishes on time. The run ends.

Sampling every 100 s gives snapshots at 0, 100 and 200 s and two period moves.
Holding is the left rectangle on the sampled level (D-141): level at the start of the
interval x holding per unit-day x interval days.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.engines.des import DesStateObserver
from twinflow.engines.des_ledger import des_economics, period_ledger
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.plan.observers import OrderView
from twinflow.state.economics import parse_economics
from twinflow.state.recorder import read_moves
from twinflow.state.records import Ledger, Snapshot

_MODEL = """
defaults: {cycle_time_cv: 0.0}
stocks:
  - {name: glue, uom: kg, initial: 10}
part_types:
  - {name: raw-a, uom: piece, attributes: {}}
  - {name: widget-a, uom: piece, attributes: {}}
  - {name: raw-b, uom: piece, attributes: {}}
  - {name: widget-b, uom: piece, attributes: {}}
machines: [{name: press}]
labor: {pools: [{name: floor, skills: [operator], headcount: 2}]}
processes: []
bom: []
locations:
  - name: mold-a
    consumes: [{thing: raw-a, qty: 1, uom: piece}]
    emits: [{thing: widget-a, qty: 1, uom: piece}]
    setup_key: grp-a
    material: {stock: glue, qty: 2, uom: kg}
    time_model: {kind: rate_based, rate: 0.01}
    machine: press
    labor_skill: operator
  - name: mold-b
    consumes: [{thing: raw-b, qty: 1, uom: piece}]
    emits: [{thing: widget-b, qty: 1, uom: piece}]
    setup_key: grp-b
    material: {stock: glue, qty: 2, uom: kg}
    time_model: {kind: rate_based, rate: 0.01}
    machine: press
    labor_skill: operator
routing:
  - {part: widget-a, steps: [mold-a]}
  - {part: widget-b, steps: [mold-b]}
"""

ECONOMICS = {
    "things": {
        "widget-a": {"price": 30, "unit_cost": 10, "late_cost_per_day": 1728},
        "widget-b": {"price": 40, "unit_cost": 15, "late_cost_per_day": 1728},
        "glue": {"holding_per_unit_day": 864},
    },
    "setup": {"cost": 50},
}

PLAN = [
    WorkOrder("A", "widget-a", 1, "0", "50"),
    WorkOrder("B", "widget-b", 1, "0", "1000"),
]


def _run(tmp_path: Path, economics: object) -> list:
    path = tmp_path / "model.yaml"
    path.write_text(_MODEL, encoding="utf-8")
    compiled = load_model(str(path))
    parsed, _ = parse_economics(economics)
    observer = DesStateObserver(every_hours=100 / 3600, economics=parsed)
    result = RunDriver(compiled).run(
        PLAN, seed=1, replication_index=0, artifact_dir=tmp_path / "runs", observers=[observer]
    )
    assert result.horizon == pytest.approx(200.0)
    assert result.artifact_dir is not None
    return read_moves(result.artifact_dir)


def test_hand_checked_ledger_on_a_two_location_floor(tmp_path: Path) -> None:
    first, second = _run(tmp_path, ECONOMICS)
    assert (first.before.t, first.after.t, second.after.t) == pytest.approx((0, 100, 200))
    assert first.before.stock == {"glue": 8.0}
    assert first.before.setup == {"press": "grp-a"}
    assert first.after.setup == {"press": "grp-b"}

    # 0 -> 100 s: hold 8 kg x $864/kg-day x 100/86400 day = $8; one setup change = $50;
    # A sells 1 x $30, costs 1 x $10 to make, 50 s late x $1728/day = $1.
    assert first.ledger == Ledger(revenue=30, production=10, setup=50, holding=8, late=1)
    assert first.events == {"orders_done": 1, "done.widget-a": 1}
    # 100 -> 200 s: hold 6 kg = $6; no setup change; B sells $40, costs $15, on time.
    assert second.ledger == Ledger(revenue=40, production=15, holding=6)
    assert second.events == {"orders_done": 1, "done.widget-b": 1}
    assert (first.ledger + second.ledger).total == pytest.approx(70 - 25 - 50 - 14 - 1)


def test_missing_economics_gives_a_zero_ledger_and_info_issues(tmp_path: Path) -> None:
    moves = _run(tmp_path, None)
    assert moves and all(move.ledger == Ledger() for move in moves)
    economics, issues = des_economics({"stocks": []})
    assert economics.things == {}
    assert issues and {issue.severity for issue in issues} == {"info"}
    assert issues[0].path == "economics"


def test_des_economics_reads_the_model_section_and_knows_its_things() -> None:
    model = {
        "stocks": [{"name": "glue"}],
        "part_types": [{"name": "widget-a"}, {"name": "widget-b"}],
        "economics": {"things": {"widget-a": {"price": 1}, "nope": {"price": 2}}},
    }
    economics, issues = des_economics(model)
    assert economics.thing("widget-a").price == 1
    errors = [issue for issue in issues if issue.severity == "error"]
    assert [issue.path for issue in errors] == ["economics.things.nope"]
    infos = {issue.path for issue in issues if issue.severity == "info"}
    assert "economics.things.*.unit_cost" in infos


def test_period_ledger_counts_only_orders_completed_inside_the_interval() -> None:
    economics, _ = parse_economics({"things": {"w": {"price": 5}}})
    before = Snapshot(t=100, period=1, setup={"m": None})
    after = Snapshot(t=200, period=2, setup={"m": "g"})
    orders = (
        OrderView("early", "w", 1, 1e9, 1, 100.0),  # finished at the interval start: earlier
        OrderView("in", "w", 2, 1e9, 2, 150.0),
        OrderView("edge", "w", 3, 1e9, 3, 200.0),  # finished at the interval end: counts
        OrderView("open", "w", 4, 1e9, 1, None),
    )
    ledger, events = period_ledger(before, after, orders, economics)
    assert ledger.revenue == 25  # (2 + 3) x $5
    assert ledger.setup == 0  # setup cost not given
    assert events == {"orders_done": 2, "done.w": 5}
