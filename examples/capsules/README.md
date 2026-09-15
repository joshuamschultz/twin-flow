# Scenario capsules

A `.twin.yaml` (or `.json`) capsule is a single file that bundles everything
needed to run and audit one scenario:

- **`model`** &mdash; the domain model itself (an office workflow, a
  supply-chain network, or a manufacturing floor).
- **`snapshot`** &mdash; the as-of state the model runs from (e.g. current
  work-in-progress, current inventory).
- **`experiment`** &mdash; how to run it (replications, seed).
- **`assumptions`** &mdash; the caveats a reader needs before trusting the
  numbers.
- **`provenance`** &mdash; where the capsule came from (imported from a
  legacy model, hand-authored, branched from another capsule).

Capsules are how a scenario travels as one portable, versionable file
instead of a model file plus a separate plan file plus tribal knowledge
about how it was meant to be run.

## The four capsules here

| File | Domain | What it is |
|---|---|---|
| `office.twin.yaml` | `office` | A four-task quote workflow (intake &rarr; legal/finance review &rarr; release) staffed by one analyst. |
| `supply-manufacturing.twin.yaml` | `supply_chain` | A single plant fed by three supplier tiers, two of them carrying port delay risk. |
| `supply-distribution.twin.yaml` | `supply_chain` | A distribution center fed by two carriers, both carrying port-closure delay risk. |
| `spring.twin.yaml` | (manufacturing, no `domain` field) | The same spring floor as [`examples/spring/`](../spring/), repackaged as a capsule instead of `model.yaml` + `plan.csv`. |

Every manufacturing floor folder (`examples/cnc-shop/`, `examples/spring/`,
and the rest) also ships its own `<name>.twin.yaml`, built from that folder's
`model.yaml` + `plan.csv`. Those are the files to import in the workspace UI.

## Run them

```bash
twinflow scenario validate examples/capsules/office.twin.yaml
twinflow scenario run examples/capsules/office.twin.yaml --seed 42
twinflow scenario export examples/capsules/office.twin.yaml --out office-exported.json
```

`scenario run` without `--out` writes into `runs/` at the repo root, same as
`twinflow run`; pass `--out <dir>` to send the replication artifacts
somewhere else. `--seed` and `--reps` are optional &mdash; when omitted,
each is read from the capsule's own `experiment` block.

Every command above works the same way against any of the four files:
swap `office.twin.yaml` for `spring.twin.yaml`,
`supply-manufacturing.twin.yaml`, or `supply-distribution.twin.yaml`.

## Loading them in the workspace UI

Start the service and the web app (see [`examples/README.md`](../README.md)
for the exact commands), then open the workspace. Three of the four
capsules appear as example cards you can load with one click:

- **Office**
- **Supply Manufacturing**
- **Supply Distribution**

`spring.twin.yaml` is intentionally **not** listed in the UI &mdash; the
legacy `spring` example folder already appears there (as **Spring**, from
its `model.yaml` + `plan.csv`), so the capsule would be a duplicate entry
for the same floor.

## Importing your own capsule

The workspace API accepts any capsule content directly, without it living
in the `examples/capsules/` folder first:

```
POST /api/workspace/scenarios
{ "name": "My scenario", "content": "<the capsule's JSON or YAML text>" }
```

This is the same import path the UI's "load example" button uses under the
hood &mdash; it reads the capsule's `content` string, validates it, and
stores it as a new scenario in the workspace, ready to run or branch from.
