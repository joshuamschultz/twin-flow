# Production capability guide

`twinflow.production` is the typed deterministic planning surface introduced in
`0.4.0a1`. It is intentionally separate from `twinflow.model`/`twinflow.plan`'s
configuration driven DES and from the optional REST, SDK, MCP, and web surfaces.
Those existing contracts remain compatible; importing this namespace does not add
service or browser support.

The public flow is:

1. Build a `ProductionProblem` from immutable `Resource`, `TimeWindow`, `Phase`,
   `ResourceUse`, `RecipeAlternative`, and `ProductionOperation` values. Add
   `ProductionJob`, `ScenarioCohort`, and `ThermalRecipe` values when those semantics
   are known.
2. Seed a `LotLedger` with `OrderDemand` and `FlowLot` records. Seed a `MaterialLedger`
   with measured `MaterialLot` records, including item, specification, UOM, quality, and
   availability time. Register external operations and qualification workflows in
   `RuntimeInputs`.
3. Call `twinflow.production.runtime.run(problem, inputs, solver="baseline")`.
   Runtime expands qualification trials, material readiness, active WIP, and external
   receipt operations before calling the rich scheduler. It verifies the schedule before
   committing ledger transitions.
4. Read `ProductionRun.schedule`, `verification`, `quantity_balances`, qualification and
   receipt records, and completion fields. `FEASIBLE` describes the executable subgraph;
   `ProductionRun.completed` says whether every requested obligation resolved.

## Minimal scheduler example

```python
from twinflow.production.contracts import (
    Phase,
    ProductionOperation,
    ProductionProblem,
    RecipeAlternative,
    Resource,
    ResourceUse,
    TimeWindow,
)
from twinflow.production.scheduling import solve, verify

machine = Resource("mill-1", "machine", windows=(TimeWindow(0, 480),))
operation = ProductionOperation(
    id="WO-1:0010",
    order_id="WO-1",
    alternatives=(
        RecipeAlternative(
            id="WO-1:0010:r1",
            primary_resource_id="mill-1",
            revision="r1",
            phases=(Phase("run", 60),),
            uses=(ResourceUse(("mill-1",), "run", "run"),),
        ),
    ),
    lot_ids=("lot-1",),
)
problem = ProductionProblem((machine,), (operation,))
schedule = solve(problem)
assert schedule.status == "FEASIBLE"
assert verify(problem, schedule).valid
```

Times in the rich scheduler are finite numbers in the problem's declared
`time_unit` (default `minutes`). Quantities always carry a UOM. A resource's `kind`
and qualifications are independent of operation recipe identity. Each phase can use
multiple resources; `hold_during_pause` distinguishes a machine occupation from an
attended phase. Interruptible phases can carry a `RestartRule`. Thermal alternatives
carry a `BatchRequirement`, and the verifier checks recipe compatibility and capacity.

The baseline places work deterministically at the earliest feasible time. A `FEASIBLE`
schedule has passed the independent verifier, but is not an optimality certificate or
plant validation. If the placement policy cannot establish a result, it returns
`UNKNOWN`. The older `twinflow.scheduling` CP-SAT interface remains separate.

## State ledgers

- `LotLedger.demand`, `seed`, `split`, `transfer`, `accept`, `scrap`, `resume_time`,
  `balance`, and `reconcile` preserve order attribution and expose contradictory WIP.
  Terminal `accepted` and `scrap` lots cannot be resurrected.
- `MaterialLedger.add_lot`, `reserve`, `consume`, `release`, `reweigh`, and
  `earliest_ready_time` require explicit per-lot UOM quantities. Reservations are atomic
  across requirements. Consumption entries must exactly match canonical allocations;
  replaying an event is idempotent only when its payload is unchanged.
- `QualificationPlan` freezes nested acceptance and shortcut evidence. A
  `QualificationState` advances through `start_trial` and `record_result`; production is
  ready only after strict Boolean pass plus finite in-spec measurements, or an explicitly
  approved shortcut with evidence. Runtime returns one `QualificationSampleAccount` per
  attempt. Samples remain outside customer demand and become materially quantified only
  when the caller supplies explicit requirements and consumptions for the generated or
  template operation IDs.
- `ExternalLedger.register`, `dispatch`, `receive`, and `customer_ship` keep supplier
  movement, accepted quantity, loss, and customer shipment separate. Stage clocks enforce
  chronology and a partial accepted receipt can become a downstream lot while the balance
  remains in transit. An `ExternalWorkflow` may use declared receipt times with unknown
  vendor capacity or attach a resource-bound `vendor_operation` for known capacity and
  calendar constraints.

## Minimal integrated run

The scheduler example above can be executed through the runtime with attributed lot
state:

```python
from twinflow.production import RuntimeInputs, run
from twinflow.production.external import ExternalLedger
from twinflow.production.lots import FlowLot, LotLedger
from twinflow.production.materials import MaterialLedger

lots = LotLedger()
lots.demand("WO-1", 1, "piece")
lots.seed(FlowLot("lot-1", "WO-1", "WO-1:0010", 1, "piece", "queued"))

result = run(
    problem,
    RuntimeInputs(lots, MaterialLedger(), ExternalLedger()),
)
assert result.schedule.status == "FEASIBLE"
assert result.verification.valid
assert result.completed
assert result.quantity_balances["WO-1"].accepted_qty == 1
```

## Runtime transaction boundary

`runtime.run` acquires all participating ledger locks in a stable order. It snapshots
mutable ledger containers without copying their locks, expands and verifies the schedule,
then applies material, external, and terminal lot transitions. Any invalid workflow,
infeasible schedule, failed reservation, or verification error restores the snapshot and
leaves qualification, material, external, and lot state unchanged.

Expected unfinished work is a valid partial result. `requested_problem` retains the full
request while `expanded_problem` contains the verified executable subgraph.
`pending_operation_ids`, `unresolved_job_ids`, and `unresolved_obligations` explain what
did not complete. An executed subset receives a distinct cohort, and
`customer_on_time_fraction` is `None` while a promised customer job is unresolved. The
runtime does not fabricate a terminal operation, accepted quantity, or service result to
close the request.

Use [`VALIDATION.md`](VALIDATION.md) for the ten-spec traceability matrix and explicit
alpha boundaries. The original requirements and historical source evidence remain in
[`SPEC.md`](SPEC.md).
