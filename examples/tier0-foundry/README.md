# Tier 0 Foundry

A single worked floor that composes **all four Tier 0 capabilities** at once, each
declared purely in `model.yaml` (the engine has no per-client code — D-044).

| Capability | Where it lives in `model.yaml` | What it does |
|---|---|---|
| **Changeover time** | `melt.changeover_seconds: 120` | The furnace charges a real 120 s setup when it first heats up. This time elapses in the simulation and shows up in the `setup_hours` KPI (which was hardcoded to 0 before Tier 0). |
| **Reorder-point material** | `sand` stock + `mold.material` | Every mold pulls 3 kg of `sand` from a finite stock that **starts empty** and refills itself up to `refill_to: 200` whenever a pull would drop it below `reorder_point: 20`. Without it the run would deadlock on the first mold. |
| **Batch / hold** | `anneal.batch_size` + `batch_hold` | The annealing oven accumulates a batch of 5 castings, then holds the **whole group** for one 400 s hold — not a per-unit time. |
| **Quality gate** | `inspect.quality_gate` | Each whole part goes down a pass/fail branch by chance: 90% finish, 10% divert to `scrap_bin`. Distinct from a fixed scrap rate — it routes whole units, drawing from the dedicated `SOURCE_ROUTING` stream so the split is seeded and reproducible. |

## Flow

```
ingot → [melt] → molten → [mold (+sand)] → casting
      → [anneal, batch hold] → annealed → [inspect, quality gate]
      → part (pass, 90%)  |  scrap_bin (fail, 10%)
```

## Run it

```bash
twinflow validate examples/tier0-foundry/model.yaml
twinflow run examples/tier0-foundry/model.yaml --plan examples/tier0-foundry/plan.csv --reps 20
```

The report's headline shows a nonzero **setup_hours** (the furnace changeover), and
because `--reps > 1` every KPI comes back as a mean with a low-to-high confidence
band across the replications.

## What this shows

- The four capabilities are **framework-wide**, not foundry-specific: each is ordinary
  config the Layer-2 compiler turns into pure primitives, so any discrete floor can use
  them.
- Reproducibility holds: the quality gate draws from its own seeded RNG stream, so the
  same seed always produces the same pass/fail split (common random numbers, D-033).

## Sweep the levers

`examples/tier0-foundry/sweep.json` grids the mold station's machine count:

```json
{ "locations[mold].capacity": [1, 2, 3] }
```

```bash
twinflow balance examples/tier0-foundry/model.yaml --plan examples/tier0-foundry/plan.csv \
  --sweep examples/tier0-foundry/sweep.json --reps 10
```

This runs the floor at 1, 2, and 3 molds, 10 replications each, so you can
see how adding a second mold interacts with the other three Tier 0
capabilities already on this floor — the finite `sand` stock it draws from,
the `anneal` batch hold downstream, and the `inspect` quality gate.

## Import into the workspace UI

`tier0-foundry.twin.yaml` in this folder is the same floor and plan packaged as a
scenario capsule. Choose it in the workspace's **Import scenario** dialog, or
run it directly:

```bash
twinflow scenario validate examples/tier0-foundry/tier0-foundry.twin.yaml
twinflow scenario run examples/tier0-foundry/tier0-foundry.twin.yaml --seed 42
```

`model.yaml` on its own is not a capsule; the importer says so if you pick it.
