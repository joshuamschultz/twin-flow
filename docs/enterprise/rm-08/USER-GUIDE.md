# RM-08 Supply Network User Guide

The supply-chain domain answers a bounded question: given configured stock, receipts, BOMs,
qualified processes, evidence gates, and orders, which orders have a material-feasible delivery
date and which dependencies block the rest?

## Run the included profiles

```python
import json
from pathlib import Path
from twinflow.domain.supply_chain import evaluate

root = Path("examples/supply-chain-network")
model = json.loads((root / "manufacturing-model.json").read_text())
snapshot = json.loads((root / "manufacturing-snapshot.json").read_text())

result = evaluate(
    model,
    snapshot,
    seed=42,
    artifact_dir="runs/supply-network",
    limits={"replications": 100},
)
```

The manufacturing profile has three named supplier tiers, a two-level assembly BOM, qualified
processes, an evidence release gate, and a shared regional delay. The distribution profile has
no factory or BOM. It demonstrates that the kernel allocates incoming cases directly and applies
one correlated delay draw to receipts exposed to the same disruption.

Call `validate(model, snapshot)` before evaluation when an interactive client needs all detected
issues. Call `describe(model, snapshot)` for supported capabilities and object counts.
`SupplyChainDomain()` exposes the same three methods and `name == "supply_chain"` for the shared
domain registry. All inputs are ordinary JSON mappings and all descriptions/results are JSON
safe.

## Model fields

- `sites`: stable `id` values.
- `suppliers`: `id`, optional `site_id`, and optional `delay_risk` with nonnegative
  `minimum_days`, `maximum_days`, and `common_risk_group`.
- `items`: `id`, `kind` (`purchased`, manufactured `part`, or `assembly`), and `uom`.
- `bom_revisions`: assembly, revision/effectivity dates, and component lines. Quantities are per
  assembly unit.
- `processes`: process `id`, assembly `item_id`, and `lead_time_days`.
- `qualifications`: supplier/process pairing and validity period.
- `gates`: item-scoped required `evidence_type` and user-owned `rule_revision`.

The snapshot supplies aware timestamps and arrays named `inventory`, `receipts`, `allocations`,
`documents`, and `orders`. Inventory and receipts share item/site/quantity/unit/available date
fields. A receipt can name its supplier. `quality_status` defaults to `released`; `hold` excludes
the supply. Existing allocation rows reserve supply before new allocation.

Orders run in descending priority, then due-date and ID order. The engine consumes each physical
supply quantity at most once. Finished supply is used before building an assembly. Recursive BOM
requirements multiply through every level. A failure returns its exact dependency path and every
affected order. Configured gate output reports the rule revision and evidence IDs used.

## Result interpretation

`complete` means every order was feasible; `incomplete` preserves blocked orders and reasons;
`invalid` means execution did not start. With multiple replications, forecasts add P50/P90 dates
and on-time probability. Common risk groups correlate receipt delays within each replication.
The seed makes those draws reproducible. The evidence artifact is the same JSON result written
atomically to `supply-chain-evidence.json`.

These dates cover material availability plus configured process lead time. They are not a
detailed finite-capacity schedule. Gates only evaluate configured evidence and validity dates;
they do not interpret regulation or certify legal compliance.
