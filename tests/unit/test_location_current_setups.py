"""T-243: `Location.current_setups()` shows the setup of busy and idle machines (D-137).

A busy machine is popped from `Location._available_machines` while it fires, so reading
only that list hides it. `current_setups()` reads every machine the location owns.
"""

from __future__ import annotations

from pathlib import Path

import simpy

from twinflow.engine.rng import RngRegistry
from twinflow.instrumentation.event_log import EventLog
from twinflow.instrumentation.inventory import InventoryLog
from twinflow.instrumentation.resource_log import ResourceUsageLog
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.primitives.location import Location

_MODEL = """
defaults: {{cycle_time_cv: 0.0}}
stocks: []
part_types:
  - {{name: raw-a, uom: piece, attributes: {{}}}}
  - {{name: done-a, uom: piece, attributes: {{}}}}
machines: [{{name: press}}]
labor: {{pools: [{{name: floor, skills: [operator], headcount: 4}}]}}
processes: []
bom: []
locations:
  - name: mold
    consumes: [{{thing: raw-a, qty: 1, uom: piece}}]
    emits: [{{thing: done-a, qty: 1, uom: piece}}]
    setup_key: family-a
    changeover_seconds: 10
    time_model: {{kind: rate_based, rate: 0.01}}
    machine: press
    capacity: {capacity}
    labor_skill: operator
routing:
  - {{part: done-a, steps: [mold]}}
"""


def _floor(tmp_path: Path, capacity: int, orders: int) -> tuple[simpy.Environment, Location]:
    path = tmp_path / "model.yaml"
    path.write_text(_MODEL.format(capacity=capacity), encoding="utf-8")
    driver = RunDriver(load_model(str(path)))
    env = simpy.Environment()
    rng = RngRegistry(1, 0)
    stocks = driver._build_stocks(env, InventoryLog())
    pools = driver._build_labor_pools(env, rng)
    locations = driver._build_locations(
        env, rng, EventLog(), ResourceUsageLog(), pools, stocks, None
    )
    plan = [WorkOrder(f"WO-{i}", "done-a", 1, "0", "1000") for i in range(orders)]
    driver._release_orders(plan, env, locations, None)
    return env, locations["mold"]


def test_idle_machine_starts_with_no_setup(tmp_path: Path) -> None:
    _, location = _floor(tmp_path, capacity=1, orders=0)
    assert location.current_setups() == {"press": None}


def test_busy_machine_setup_is_visible_mid_run(tmp_path: Path) -> None:
    env, location = _floor(tmp_path, capacity=1, orders=1)
    env.run(until=50)  # after the 10 s changeover, inside the 100 s run
    assert location._available_machines == []  # the machine is busy (popped)
    assert location.current_setups() == {"press": "family-a"}
    env.run()
    assert location.current_setups() == {"press": "family-a"}


def test_every_machine_of_a_multi_machine_location_is_listed(tmp_path: Path) -> None:
    env, location = _floor(tmp_path, capacity=2, orders=1)
    env.run(until=50)
    assert location.current_setups() == {"press-1": "family-a", "press-2": None}


def test_current_setups_returns_a_copy(tmp_path: Path) -> None:
    _, location = _floor(tmp_path, capacity=1, orders=0)
    location.current_setups()["press"] = "changed"
    assert location.current_setups() == {"press": None}
