# Glossary

Every term twinflow uses, in one place. Links point to the doc that covers it in depth.

**Adapter** — a plug-in surface where an external system attaches (ERP connector,
forecaster, dispatch policy, RL environment). See [adapters.md](adapters.md).

**Batch / batch_size** — accumulate jobs to a threshold in the thing's own unit before an
operation fires. See [modeling.md](modeling.md).

**Batch hold** — one timed hold over a whole accumulated group at once (an oven, a cure).
See [modeling.md](modeling.md).

**BOM (bill of materials)** — how much raw material one finished part needs, rolled up
automatically from what each operation consumes. You never hand-write it.

**Bundle** — the unit of flow: a quantity of one part type, in one unit of measure, with
attributes. It never changes in place. See [concepts.md](concepts.md).

**Calibration** — tuning model parameters until the simulated KPIs match a real run
("reproduced my last N weeks within X%"). See [tuning.md](tuning.md).

**Capacity-N** — a work center with N identical machines in parallel (`capacity: N`).

**Changeover** — setup time charged when the next job's setup group differs from the
machine's current one (`changeover_seconds`). See [modeling.md](modeling.md).

**Confidence interval / band** — a low-to-high range (mean plus `lo`/`hi`) across
replications. The honest answer, versus a single fake-certain number. See [running.md](running.md).

**Common random numbers (CRN)** — every scenario in a comparison runs on the same random
draws, so differences are real, not noise. See [architecture.md](architecture.md).

**Cost function** — maps a scenario's config + KPIs to money (labor, capacity, lateness,
holding, stockout). See [modules.md](modules.md).

**Dispatch policy** — the rule a work center uses to pick the next job from its queue
(`fifo`, `edd`, `spt`, `critical_ratio`). See [active-control.md](active-control.md).

**Disruption** — a seeded upset the twin models: machine breakdown, operator absence, or a
rush order. See [active-control.md](active-control.md).

**Evaluation** — the scored result of running one scenario: representative KPIs plus a
confidence band (and inventory KPIs). See [modules.md](modules.md).

**Event log** — `events.parquet`, one row per firing; every KPI is derived from it. See
[data.md](data.md).

**Fill rate** — the share of orders delivered within the horizon. See [running.md](running.md).

**Horizon** — the simulated length of a run, in seconds.

**Labor pool** — a named, skilled group of operators, separate from machines. See
[concepts.md](concepts.md).

**Lead time** — the delay before a placed replenishment order arrives (`lead_time`). See
[supply-chain.md](supply-chain.md).

**Lever** — one tunable config value addressed by a dot-path (`labor.pools[0].headcount`).
Sweeps and optimizers range over levers. See [optimizing.md](optimizing.md).

**Location / work center** — the floor node that runs an operation: pull a job, grab a
machine then an operator, consume material, charge setup then run time, apply scrap, emit
outputs. See [concepts.md](concepts.md).

**Machine** — a server inside a work center that does the work and remembers its own
setup state.

**Makespan** — the time the last order completes.

**Model (`model.yaml`)** — the declarative description of a floor. See [modeling.md](modeling.md).

**Multi-echelon** — stocks that replenish from other stocks upstream (`supplier`), forming
a chain. See [supply-chain.md](supply-chain.md).

**Objective** — a scalar an optimizer ranks by (`on_time_pct`, `robust_on_time`,
`makespan`, `service_level`, ...). See [modules.md](modules.md).

**On-time %** — the share of known orders that finished by their due date.

**Optimizer** — searches the lever space under an objective (`grid`, `random`,
`hill_climb`, `genetic`). It proposes; it never commits. See [optimizing.md](optimizing.md).

**Order release** — the policy that controls when work enters the floor (`plan`,
`wip_cap`, `conwip`). See [active-control.md](active-control.md).

**Part type** — a declared thing with its unit of measure and typed attributes.

**Plan** — the production plan table (`.xlsx`/`.csv`) of work orders. See [data.md](data.md).

**Priority (rush)** — a plan column; a higher number jumps every dispatch queue. See
[active-control.md](active-control.md).

**Quality gate** — routes whole units down a pass/fail branch by chance (`quality_gate`),
distinct from a fixed scrap rate. See [modeling.md](modeling.md).

**Replication** — one run of a scenario. Many replications give the confidence band; one
replication is a lie. See [running.md](running.md).

**Reorder point** — the stock level that triggers a replenishment order (`reorder_point` /
`refill_to`). See [supply-chain.md](supply-chain.md).

**Reproducibility stamp** — the metadata that lets a run be re-created exactly. See
[architecture.md](architecture.md).

**Routing** — the ordered list of work centers a part visits.

**Scenario** — a set of lever overrides; the input to the scoring surface. See
[modules.md](modules.md).

**Scoring surface** — the one seam that turns a scenario into scored KPIs; every module
attaches to it. See [modules.md](modules.md).

**Scrap** — a declared fraction of a firing becomes a scrap output bundle.

**Service level** — a supply-chain KPI: 100 minus the average share of the run each stock
sat empty. See [supply-chain.md](supply-chain.md).

**Setup group (`setup_key`)** — parts in the same group run back to back without a
changeover.

**Stock** — a material level you pull from by name; blocks work when empty. See
[concepts.md](concepts.md).

**Stockout** — time a stock sat at level 0. See [supply-chain.md](supply-chain.md).

**Surrogate model** — a cheap learned stand-in for the scoring surface, to screen
scenarios (`linear`, `nearest_neighbor`). See [modules.md](modules.md).

**Sweep** — running a grid of lever values side by side (`twinflow balance`). See
[optimizing.md](optimizing.md).

**Time model** — how long an operation takes (`rate_based`, `distribution`,
`attribute_scaled`, `batch_hold`). See [modeling.md](modeling.md).

**Transform** — the one contract every operation uses: input bundles become output
bundles. See [concepts.md](concepts.md).

**Twin (digital twin)** — a working software copy of your operation you can run
experiments on.

**Utilization** — the share of the horizon a work center was busy.

**Variation (`cv`)** — the spread on a cycle time; makes replicated runs report a range.
See [tuning.md](tuning.md).

**WIP (work in progress)** — jobs in the system. A WIP cap bounds it. See
[active-control.md](active-control.md).

**WIP over time** — a derived series of how many jobs sat at each center through the run.
