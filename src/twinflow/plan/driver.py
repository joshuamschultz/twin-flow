"""COMP-019 RunDriver — executes one terminating replication.

Seeds initial WIP, releases work orders on their start dates subject to any WIP ceiling,
returns when the plan completes. Callable in-process with no subprocess and no re-parse
(D-041): `RunDriver.__init__` takes an already-loaded `CompiledModel` once; every `run()`
call builds fresh per-replication runtime state (RunContext/env, RngRegistry, Stocks,
Locations, LaborPools, EventLog) so two runs never share mutable state and stay
independent (CRN-correct) — the `CompiledModel` itself, including every `LocationSpec`
in it, is treated as read-only config and is never mutated.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass, replace
from pathlib import Path

import simpy

from twinflow.engine.clock import RunContext
from twinflow.engine.rng import RngRegistry
from twinflow.instrumentation.event_log import EventLog
from twinflow.instrumentation.inventory import InventoryLog
from twinflow.model import CompiledModel, StockConfig
from twinflow.model.schema import LocationSpec
from twinflow.plan.loader import WorkOrder
from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine
from twinflow.primitives.labor import LaborPool
from twinflow.primitives.location import Location, MaterialRequirementLike, RoutingPolicy
from twinflow.primitives.stock import Stock


@dataclass(frozen=True)
class RunResult:
    """One replication's outcome: where the event log landed, how long simulated
    time ran, and the run's reproducibility metadata."""

    event_log_path: Path
    horizon: float
    run_meta: dict[str, object]


class _AlwaysOnShiftCalendar:
    """Default `primitives.labor.ShiftCalendar`: `model.yaml`'s `labor.pools`
    entries declare no shift calendar yet, so every pool is treated as always on
    shift — the same "no client data yet, so default to the least-restrictive
    option" pattern D-027/D-021 already apply elsewhere. Flagged as an assumption
    for the (not-yet-built) COMP-025 AssumptionsCollector to name.
    """

    def is_on_shift(self, t: float) -> bool:
        return True

    def next_shift_start(self, t: float) -> float:  # pragma: no cover - never off-shift
        return t


@dataclass
class _BoundMaterial:
    """Runtime binding of a compiled `MaterialSpec` to a fresh, env-bound `Stock`.

    Satisfies `primitives.location.MaterialRequirementLike` structurally (stock,
    qty, thing, uom) so `Location._fire`'s step-3 material pull works unchanged.
    """

    stock: Stock
    qty: float
    thing: str
    uom: str


class _RoutingSink(list[Bundle]):
    """A `destinations[thing]` list sink that also forwards each appended bundle
    to the next Location in the compiled routing chain.

    `Location._fire`'s output step is duck-typed on `.append()` only (never
    `isinstance(..., list)`), so this list subclass satisfies COMP-007's
    `LocationSpecLike.destinations` contract unchanged while additionally
    routing the bundle onward — no primitives/model change needed.
    """

    def __init__(self, downstream: Location) -> None:
        super().__init__()
        self._downstream = downstream

    def append(self, bundle: Bundle) -> None:
        super().append(bundle)
        self._downstream.enqueue(bundle)


def _delayed_enqueue(
    env: simpy.Environment, location: Location, bundle: Bundle, start_seconds: float
) -> Generator[simpy.Event, None, None]:
    """Release `bundle` onto `location`'s queue `start_seconds` after run start."""
    yield env.timeout(start_seconds)
    location.enqueue(bundle)


class RunDriver:
    """Executes one terminating replication against an already-loaded model.

    `__init__` stores the `CompiledModel` and derives the read-only, compile-time
    lookups every `run()` call needs (per-location spec, skill -> labor pool
    name). `run()` never re-parses or re-compiles `model.yaml` and never spawns a
    subprocess (D-041) — it is pure in-process `env.run()`.
    """

    def __init__(self, compiled: CompiledModel) -> None:
        self._compiled = compiled
        self._location_specs: dict[str, LocationSpec] = {
            spec.location_id: spec for spec in compiled.locations
        }
        self._pool_name_by_skill: dict[str, str] = {
            skill: pool.name for pool in compiled.labor_pools for skill in pool.skills
        }

    def run(self, plan: list[WorkOrder], seed: int, replication_index: int) -> RunResult:
        """Run one terminating replication: seed WIP, release orders, simulate.

        Builds a FRESH RunContext(env), RngRegistry, EventLog, LaborPools and
        Locations for this call only — nothing built here is reused by a later
        `run()` call, which is what keeps replications independent (CRN-correct)
        even though they share the same `CompiledModel`.
        """
        run_id = f"run-{seed}-{replication_index}-{uuid.uuid4().hex[:8]}"
        ctx = RunContext(run_id=run_id, seed=seed, horizon=0.0)
        env = ctx.env
        rng = RngRegistry(seed, replication_index)
        event_log = EventLog()
        inventory_log = InventoryLog()

        stocks = self._build_stocks(env, inventory_log)
        labor_pools = self._build_labor_pools(env)
        locations = self._build_locations(env, rng, event_log, labor_pools, stocks)

        for order in plan:
            self._seed_wip(order, locations)
        for order in plan:
            self._schedule_release(order, env, locations)

        env.run()

        run_dir = Path("runs") / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        event_log_path = event_log.flush(run_dir)
        inventory_log.flush(run_dir)  # always written next to events.parquet (may be empty)

        run_meta: dict[str, object] = {
            "run_id": run_id,
            "seed": seed,
            "replication_index": replication_index,
            "stock_levels": {name: stock.level for name, stock in stocks.items()},
        }
        return RunResult(event_log_path=event_log_path, horizon=env.now, run_meta=run_meta)

    # -- fresh per-run runtime construction ---------------------------------

    def _build_stocks(
        self, env: simpy.Environment, inventory_log: InventoryLog
    ) -> dict[str, Stock]:
        """Fresh `Stock`s from the compiled declarative shape — env-bound, so
        never built at `load_model()` time (only here, once per `run()` call).
        A stock's `name` doubles as its material identity (D-044).

        Built in two passes: first every `Stock` (with an inventory-logging
        `on_level_change` observer and a seeded initial-level row), then the reorder
        hooks — set second because a multi-echelon hook references the whole stocks
        map. A stock declaring `reorder_point`/`refill_to` places an order-up-to
        replenishment that arrives after `lead_time` (instantaneous when `lead_time`
        is 0), optionally pulling the order quantity from a `supplier` stock. The
        hooks are pure Layer-3 policy — the `Stock` primitive stays reorder-agnostic.
        """
        recorder = self._inventory_recorder(inventory_log)
        stocks: dict[str, Stock] = {}
        for config in self._compiled.stocks:
            stocks[config.name] = Stock(
                thing=config.name,
                uom=config.uom,
                env=env,
                initial_qty=config.initial,
                on_level_change=recorder,
            )
            inventory_log.record(config.name, env.now, config.initial, config.initial, "seed")

        for config in self._compiled.stocks:
            hook = self._make_reorder_hook(config, stocks, inventory_log, env)
            if hook is not None:
                stocks[config.name].set_reorder_hook(hook)
        return stocks

    @staticmethod
    def _inventory_recorder(
        inventory_log: InventoryLog,
    ) -> Callable[[Stock, float, str], None]:
        """A level-change observer that records `(stock, t, level-after, delta, kind)`
        to the inventory log — the pure Layer-3 side of `Stock.on_level_change`."""

        def record(stock: Stock, delta: float, kind: str) -> None:
            inventory_log.record(stock.thing, stock.env.now, stock.level, delta, kind)

        return record

    def _make_reorder_hook(
        self,
        config: StockConfig,
        stocks: dict[str, Stock],
        inventory_log: InventoryLog,
        env: simpy.Environment,
    ) -> Callable[[Stock], None] | None:
        """An (s, S) reorder callback for a self-refilling stock, or None.

        When on-hand drops below `reorder_point` and no order is in transit, place ONE
        order-up-to-`refill_to` order. With `lead_time == 0` and no supplier the refill
        is synchronous (the historical instantaneous behaviour). Otherwise the order is
        scheduled to arrive after `lead_time`, optionally drawing from the `supplier`
        stock; a single-slot `in_transit` flag prevents a second order while one is
        outstanding, and a stock that empties before delivery is a real stockout that
        blocks consumers without deadlocking (the order is already on its way).
        """
        if config.reorder_point is None or config.refill_to is None:
            return None
        reorder_point = config.reorder_point
        refill_to = config.refill_to
        lead_time = config.lead_time
        supplier = config.supplier
        name = config.name
        in_transit = [False]

        def hook(runtime_stock: Stock) -> None:
            if runtime_stock.level >= reorder_point:
                return
            order_qty = refill_to - runtime_stock.level
            if order_qty <= 0.0:
                return
            if lead_time <= 0.0 and supplier is None:
                inventory_log.record(name, env.now, runtime_stock.level, order_qty, "order")
                runtime_stock.put(
                    Bundle(qty=order_qty, thing=runtime_stock.thing, uom=runtime_stock.uom)
                )
                return
            if in_transit[0]:
                return
            in_transit[0] = True
            inventory_log.record(name, env.now, runtime_stock.level, order_qty, "order")
            env.process(
                self._deliver(runtime_stock, order_qty, lead_time, supplier, stocks, in_transit)
            )

        return hook

    @staticmethod
    def _deliver(
        runtime_stock: Stock,
        order_qty: float,
        lead_time: float,
        supplier: str | None,
        stocks: dict[str, Stock],
        in_transit: list[bool],
    ) -> Generator[simpy.Event, None, None]:
        """The in-transit replenishment: wait `lead_time`, optionally pull the order
        quantity from the `supplier` stock (a multi-echelon draw that may block on and
        trigger the upstream's own reorder), then deliver into `runtime_stock` and
        clear the in-transit flag so a future dip can order again."""
        if lead_time > 0.0:
            yield runtime_stock.env.timeout(lead_time)
        if supplier is not None:
            upstream = stocks[supplier]
            yield from upstream.pull(upstream.thing, order_qty, upstream.uom)
            upstream.review_reorder()  # the draw may have dropped the upstream below its point
        runtime_stock.put(
            Bundle(qty=order_qty, thing=runtime_stock.thing, uom=runtime_stock.uom)
        )
        in_transit[0] = False

    def _build_labor_pools(self, env: simpy.Environment) -> dict[str, LaborPool]:
        """Fresh `LaborPool`s from the compiled declarative shape — env-bound, so
        never built at `load_model()` time (only here, once per `run()` call)."""
        calendar = _AlwaysOnShiftCalendar()
        return {
            pool.name: LaborPool(
                name=pool.name,
                headcount=pool.headcount,
                skills=pool.skills,
                shift_calendar=calendar,
                env=env,
            )
            for pool in self._compiled.labor_pools
        }

    def _build_locations(
        self,
        env: simpy.Environment,
        rng: RngRegistry,
        event_log: EventLog,
        labor_pools: dict[str, LaborPool],
        stocks: dict[str, Stock],
    ) -> dict[str, Location]:
        """Fresh `Location`s for this run: each gets its own `Machine` (mutable
        `current_setup`) and its own `destinations` lists, so a run can never
        leave state behind for the next one (the replication-independence fix)."""
        fresh_specs: dict[str, LocationSpec] = {
            spec.location_id: replace(
                spec,
                machine=Machine(machine_id=spec.machine.machine_id),
                destinations={thing: [] for thing in spec.destinations},
                routers={},
                material_requirement=self._bind_material(spec, stocks),
            )
            for spec in self._compiled.locations
        }

        locations: dict[str, Location] = {}
        for location_id, spec in fresh_specs.items():
            machine_pool = simpy.PriorityResource(env, capacity=spec.capacity)
            labor_pool = labor_pools[self._pool_name_by_skill[spec.labor_skill]]
            locations[location_id] = Location(spec, env, machine_pool, labor_pool, event_log, rng)

        self._wire_routing(fresh_specs, locations)
        self._wire_stock_destinations(fresh_specs, stocks)
        self._wire_quality_gates(fresh_specs, locations, stocks)

        for location in locations.values():
            env.process(location.run())

        return locations

    @staticmethod
    def _bind_material(
        spec: LocationSpec, stocks: dict[str, Stock]
    ) -> MaterialRequirementLike | None:
        """Bind a compiled `material_spec` to a fresh, env-bound `Stock` so the
        Location's step-3 material pull runs against real state this run (D-044)."""
        material = spec.material_spec
        if material is None:
            return None
        return _BoundMaterial(
            stock=stocks[material.stock],
            qty=material.qty,
            thing=material.stock,
            uom=material.uom,
        )

    def _wire_quality_gates(
        self,
        specs: dict[str, LocationSpec],
        locations: dict[str, Location],
        stocks: dict[str, Stock],
    ) -> None:
        """Resolve each declared `quality_gate` into a run-bound `RoutingPolicy`
        on the fresh spec's `routers`. A branch's `to` names a downstream Location
        (routed onward via `_RoutingSink`), a `Stock` (put into), or is null (a
        terminal list sink). Mirrors `_wire_routing`'s post-compile rewrite."""
        for spec in specs.values():
            gate = spec.quality_gate
            if gate is None:
                continue
            branches: list[tuple[float, Stock | list[Bundle]]] = []
            for branch in gate.branches:
                sink = self._resolve_branch_sink(branch.to, locations, stocks)
                branches.append((branch.prob, sink))
            spec.routers[gate.thing] = RoutingPolicy(branches)

    @staticmethod
    def _resolve_branch_sink(
        to: str | None,
        locations: dict[str, Location],
        stocks: dict[str, Stock],
    ) -> Stock | list[Bundle]:
        """A quality-gate branch target -> a run-bound sink: a Location (routed
        onward), a Stock (put into), or a terminal list sink when `to` is null."""
        if to is None:
            return []
        if to in locations:
            return _RoutingSink(locations[to])
        if to in stocks:
            return stocks[to]
        raise KeyError(f"quality_gate branch names unknown target {to!r}")

    def _wire_stock_destinations(
        self, specs: dict[str, LocationSpec], stocks: dict[str, Stock]
    ) -> None:
        """Rewrite each declared `output_stocks` thing's `destinations[thing]`
        to the matching fresh, env-bound `Stock` built this run — mirrors
        `_wire_routing`'s post-compile `destinations` rewrite for Location ->
        Location routing (D-044). A thing with no `stock_destinations` entry
        keeps its existing routing/default sink."""
        for spec in specs.values():
            for thing, stock_name in spec.stock_destinations.items():
                spec.destinations[thing] = stocks[stock_name]

    def _wire_routing(self, specs: dict[str, LocationSpec], locations: dict[str, Location]) -> None:
        """Rewrite each non-terminal output's `destinations[thing]` to a
        `_RoutingSink` pointed at the next step's Location, per the compiled
        `routing` graph (part -> ordered location ids). A step's output that no
        following step consumes (a terminal/finished-part or scrap output) is
        left pointed at its default plain-list sink."""
        for steps in self._compiled.routing.values():
            for current_id, next_id in zip(steps, steps[1:], strict=False):
                current_spec = specs[current_id]
                next_inputs = set(specs[next_id].pull_rule.setup_key_of)
                for thing in list(current_spec.destinations):
                    if thing in next_inputs:
                        current_spec.destinations[thing] = _RoutingSink(locations[next_id])

    # -- plan interpretation --------------------------------------------------

    def _seed_wip(self, order: WorkOrder, locations: dict[str, Location]) -> None:
        """Seed declared initial WIP directly onto its named Location, before the
        run advances (t=0, called before `env.run()`), per the committed
        convention: `Bundle(qty=initial_wip_qty, thing=<the receiving location's
        own consumed thing>, uom=<matching uom>)`."""
        if order.initial_wip_location is None:
            return
        spec = self._location_specs[order.initial_wip_location]
        thing = next(iter(spec.pull_rule.setup_key_of))
        uom = self._compiled.registry.uom(thing)
        qty = float(order.initial_wip_qty or 0)
        locations[order.initial_wip_location].enqueue(Bundle(qty=qty, thing=thing, uom=uom))

    def _schedule_release(
        self, order: WorkOrder, env: simpy.Environment, locations: dict[str, Location]
    ) -> None:
        """Release `order`'s demand on its declared `start_date` (non-negative
        simulation-seconds offset from run start). A zero-qty row (e.g. a
        WIP-only row) releases nothing."""
        if order.qty <= 0:
            return
        start_seconds = float(order.start_date)
        for location_id, bundle in self._demand_release_bundles(order):
            env.process(_delayed_enqueue(env, locations[location_id], bundle, start_seconds))

    def _demand_release_bundles(self, order: WorkOrder) -> list[tuple[str, Bundle]]:
        """Resolve one demand WorkOrder into (location_id, Bundle) release
        targets: one release per raw material in the finished part's rolled-up
        BOM (COMP-016 `CompileResult.bom`, which already resolves down to true
        raw materials only), injected at whichever location in the routing chain
        directly consumes it. Downstream locations receive their input material
        through the routing wiring, never a second manual release."""
        steps = self._compiled.routing[order.part]
        consumed_by: dict[str, str] = {}
        for location_id in steps:
            for thing in self._location_specs[location_id].pull_rule.setup_key_of:
                consumed_by.setdefault(thing, location_id)

        releases: list[tuple[str, Bundle]] = []
        for raw_thing, qty_per_unit in self._compiled.bom[order.part].items():
            location_id = consumed_by[raw_thing]
            uom = self._compiled.registry.uom(raw_thing)
            qty = order.qty * qty_per_unit
            releases.append((location_id, Bundle(qty=qty, thing=raw_thing, uom=uom)))
        return releases
