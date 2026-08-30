# Changelog

All notable changes to twinflow are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims at
[semantic versioning](https://semver.org/).

## [0.2.0-alpha] — 2026-08-29

The **alpha release**: the single-floor engine gains the last of its v1 physics, a
pluggable module surface for optimization and machine learning, a local REST API, and
a React front end. The engine still has no per-client code — every new capability is
declarative config compiled into pure primitives (D-044).

### Added — engine (Tier 0 physics)

- **Changeover time.** A work center charges a real setup when the next job's setup
  group differs from the machine's current one, via `changeover_seconds: N` on a
  location. The time elapses in simulation and is now reported: a new `setup_seconds`
  event-log column and a real `setup_hours` KPI (previously hardcoded to 0).
- **Reorder-point / self-refilling stock.** A stock declares
  `{reorder_point, refill_to, initial}`; a driver-owned monitor tops it up the instant
  a pull would drop it below the reorder point, so a finite feedstock never deadlocks.
  A location can pull a secondary material with `material: {stock, qty, uom}`.
- **Probabilistic quality gate.** A location declares
  `quality_gate: {thing, branches: [{prob, to}]}` whose branch probabilities sum to
  1.0; whole units are routed down a pass/fail path by chance, drawing from a dedicated
  seeded `SOURCE_ROUTING` stream (reproducible, CRN-correct). Distinct from a fixed
  scrap rate.
- **Batch / hold operation.** A new `time_model: {kind: batch_hold, seconds: N}` paired
  with `batch_size` holds a whole accumulated group for one duration (an oven, a cure,
  a cool) instead of charging per-unit time.
- **`examples/tier0-foundry/`** — a worked floor composing all four capabilities end to
  end.

### Added — module surface (`twinflow.modules`)

One unified scoring seam (`ScoringSurface`: a scenario of lever overrides in, KPIs plus
a confidence band out) that four kinds of pluggable module attach to, each in its own
registry so a new one is a `register(name, factory)` call, never a core edit:

- **Objectives** — `on_time_pct`, `robust_on_time` (a confidence-band edge, not the
  mean), `makespan`, `mean_lateness`, `utilization`, plus `WeightedObjective` and
  `CostObjective`.
- **Cost functions** — `labor_cost`, `capacity_cost`, `lateness_penalty`, `total_cost`.
- **Optimizers** — `grid`, `random`, `hill_climb`, `genetic`. They propose; they never
  commit a change to a real floor.
- **Surrogate models** — `linear` and `nearest_neighbor` (numpy-only), to learn the
  surface and screen scenarios cheaply.

### Added — supply chain & inventory

- **Supplier lead time.** A stock declares `lead_time: <seconds>`; a placed replenishment
  order arrives that many seconds later (default 0 preserves instantaneous refill). An
  (s, S) order-up-to policy with a single-order-in-transit guard; a stock that empties
  before delivery is a real stockout that blocks consumers without deadlocking.
- **Multi-echelon.** A stock declares `supplier: <stock_name>` to draw its refills from
  an upstream stock instead of an infinite external source, forming a cascading chain of
  echelons (each with its own reorder point + lead time). Supplier cycles are rejected.
- **Inventory instrumentation.** A per-run `inventory.parquet` (next to `events.parquet`)
  and `InventoryKpis`: time-weighted average level, ending level, stockout seconds,
  orders placed, and total ordered — per stock.
- **Inventory economics in the module surface.** New cost functions
  `inventory_holding_cost` and `stockout_penalty`, and a `service_level` objective, so
  reorder points and order-up-to levels can be *optimized* by the existing optimizers
  (they are ordinary levers). `examples/supply-chain/` is a two-echelon worked floor.

### Added — active control (Phase A)

Turns the passive twin into a decision tool. Each is config-only, seeded, and
reproducible, and each is an ordinary lever a sweep or optimizer can search.

- **Dispatch rules.** `dispatch: fifo|edd|spt|critical_ratio` on a location re-sequences
  its ready queue (default `fifo` = today's arrival order). Wired natively in the engine
  (mirroring `adapters.dispatch` without a layering violation); due date + priority ride
  on the bundle so downstream centers still see them.
- **Order release control.** Model-level `release: {policy: plan|wip_cap|conwip, wip_cap: N}`
  holds new releases while the floor is at its WIP cap (default `plan` = release on
  start date).
- **Disruptions.** Machine `breakdown: {mtbf, mttr}` (seeded failure/repair, downtime in
  run metadata), per-pool `absence_rate` (a seeded share of headcount unavailable), and
  rush orders via an optional `priority` plan column that jumps every queue. New
  `SOURCE_ABSENCE` RNG stream (breakdown already had its own). `examples/active-control/`
  composes all of it.
- Deferred honestly: a resource-assignment policy (A3), a mid-run decide-act loop (A5),
  and surfacing downtime as an aggregated confidence band.

### Added — trust loop: calibration (Phase B)

- **`calibrate()`** tunes model parameters until the simulated KPIs match a real run.
  It is *just optimization* over the module surface — a `CalibrationTarget` (an observed
  `KpiSet` + per-metric weights) becomes a minimise objective, and any optimizer searches
  the declared parameter levers. Returns the best-fit parameters, the distance, and
  whether it is within tolerance — the "reproduced my last N weeks within X%" workflow.

### Added — integration surfaces (`twinflow.adapters`)

Unified seams so a real ERP, RL agent, or forecaster plugs in with one `register(...)`
call — the concrete integrations are deliberately out of scope, this is what they attach
to. Discoverable at `GET /api/adapters`.

- **Connectors** — `PlanSource` / `ResultSink` / `ActualSource` with a `FieldMapping`
  onto the canonical plan contract (CSV / JSON / parquet references).
- **Demand generation** — `DemandGenerator` (`fixed`, `poisson`) produces reproducible
  plans instead of only importing them.
- **Forecasting** — `Forecaster` (`naive`, `moving_average`); Prophet/ARIMA plug in.
- **Dispatch policies** — `fifo`, `edd`, `spt`, `critical_ratio` (the scheduling seam).
- **RL environment** — `TwinEnv`, a Gymnasium-style env over the twin; `to_gymnasium()`
  adapts it to a real `gymnasium.Env` so stable-baselines3 plugs straight in.
- **Plan-vs-actual** reconciliation (`reconcile`) and **orders/deliveries** fulfillment
  KPIs (fill rate, on-time delivery, backorders).

### Added — service + front end

- **`twinflow.service`** — an optional FastAPI app (`pip install -e ".[api]"`) exposing
  model discovery, a floor graph, tunable lever suggestions, module discovery, and
  run / sweep / optimize as background jobs the UI polls.
- **`web/`** — a React + TypeScript + Vite front end: floor map, and Run / Sweep /
  Optimize tabs where the confidence band is the central visual.
- **CLI** — new `twinflow optimize` and `twinflow serve` commands.

### Fixed

- Optimizers could spin forever when the search space was smaller than the budget
  (cache hits never advanced the budget). The evaluator now enforces a hard attempt
  ceiling, so every optimizer is guaranteed to terminate while `budget` still means
  "real evaluations."

## [0.1.0] — v1 engine

- Config-only single-floor digital twin; SimPy discrete-event engine; per-source RNG
  streams with common random numbers; cycle-time distributions; confidence intervals
  across replications; capacity-N work centers; KPI suite; what-if lever sweeps;
  self-contained HTML report + JSON sidecar; `twinflow` CLI (`validate`, `run`,
  `balance`, `report`).
