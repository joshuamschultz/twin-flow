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
2. Call `solve(problem)` and check it with `verify(problem, schedule)` from
   `twinflow.production.scheduling`. `compare_schedules` compares two schedules.

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

## Removed in TF-001

The production execution ledgers and runtime were removed in TF-001: `LotLedger`,
`MaterialLedger`, qualification, `ExternalLedger`, state validation, and
`runtime.run` with `ProductionRun` and `RuntimeInputs`. `twinflow.production` now keeps
only `contracts`, `scheduling` (`solve`, `validate`, `verify`, `compare_schedules`) and
`validation`, until TF-002 absorbs them into `scheduling/`. The removed code is
reachable at git tag `pre-cleanup`.

Use [`VALIDATION.md`](VALIDATION.md) for the ten-spec traceability matrix and explicit
alpha boundaries. The original requirements and historical source evidence remain in
[`SPEC.md`](SPEC.md).
