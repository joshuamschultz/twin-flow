# Supply-chain network worked example

Two JSON profiles for the `supply_chain` domain (see
[`docs/enterprise/rm-08/USER-GUIDE.md`](../../docs/enterprise/rm-08/USER-GUIDE.md)
for the full field reference). Each profile is a `model` + `snapshot` pair:
the model is the static network (sites, suppliers, items, BOMs, processes,
gates), the snapshot is a point-in-time state (inventory, receipts,
allocations, documents, orders).

This domain answers one bounded question: given the configured network and
current state, which orders have a material-feasible delivery date, and
which dependencies block the rest?

## The two profiles

- **`manufacturing-model.json` / `manufacturing-snapshot.json`** &mdash; one
  plant, three supplier tiers (one carrying regional port delay risk), a
  two-level assembly BOM, a qualified process, and an evidence release
  gate. One order.
- **`distribution-model.json` / `distribution-snapshot.json`** &mdash; one
  distribution center, two carriers (both sharing a `port-closure`
  correlated delay risk), no factory and no BOM. This profile shows the
  kernel allocating incoming cases directly and applying one correlated
  delay draw to every receipt exposed to that same disruption. Two orders.

## Run it

```python
import json
from pathlib import Path
from twinflow.domain.supply_chain import evaluate, validate, describe

root = Path("examples/supply-chain-network")
model = json.loads((root / "manufacturing-model.json").read_text())
snapshot = json.loads((root / "manufacturing-snapshot.json").read_text())

# describe() reports supported capabilities and object counts, cheap to call first.
print(describe(model, snapshot))

# validate() surfaces every detected issue; an empty list means the inputs are clean.
print(validate(model, snapshot))

result = evaluate(
    model,
    snapshot,
    seed=42,
    artifact_dir="runs/supply-network",
    limits={"replications": 100},
)
print(result["status"])
```

Swap in `distribution-model.json` / `distribution-snapshot.json` to run the
other profile the same way. `artifact_dir` should point somewhere outside
the repo's tracked `runs/` tree for a scratch run (for example
`/tmp/<something>`); the evidence artifact
(`supply-chain-evidence.json`) and full replication data land there.

## Reading the result

- **`status`** &mdash; `complete` means every order was feasible in every
  requested replication; `incomplete` preserves the blocked orders and
  their reasons; `invalid` means execution never started (bad inputs);
  `partially_feasible` (at the order level) means some replications for
  that order were blocked and others weren't.
- **`order_forecasts`** &mdash; sample, feasible, delivery-date, and
  censored counts per order, plus P50/P90 over the observed feasible
  dates.
- **`shortages`** and **`gate_results`** &mdash; unioned across all
  replications, each with an occurrence count and the replication indices
  it happened in.
- **`replication_samples`** &mdash; the full evidence, one entry per
  replication, if you need to trace a specific blocked run.

These are material-availability-plus-process-lead-time dates, not a
detailed finite-capacity schedule, and the gates only check configured
evidence and validity windows &mdash; they don't interpret regulation or
certify legal compliance.

## Other entry points

`SupplyChainDomain()` exposes the same `evaluate`/`validate`/`describe`
methods as a class with `name == "supply_chain"`, for code that looks
domains up from a shared registry instead of importing the functions
directly.

## Import into the workspace UI

`manufacturing.twin.yaml` and `distribution.twin.yaml` in this folder package
each JSON pair as a scenario capsule. Choose one in the workspace's **Import
scenario** dialog, or run it directly:

```bash
twinflow scenario validate examples/supply-chain-network/manufacturing.twin.yaml
twinflow scenario run examples/supply-chain-network/manufacturing.twin.yaml --seed 42
```

The bare `*-model.json` and `*-snapshot.json` files are inputs for the Python
API above; the importer rejects them on their own.
