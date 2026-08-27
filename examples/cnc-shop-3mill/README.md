# CNC Shop, 3 mills — a capacity-investment worked example

The same shop as [`cnc-shop`](../cnc-shop/), with one change: the CNC mill
step runs **three machines in parallel** instead of one, on each line
(`capacity: 3` on `mill_shaft` and `mill_bracket`). It is the "should we buy
more mills?" decision, modelled.

In the base shop the mill is the bottleneck: it runs flat out while every
downstream center starves, and most orders ship late. Add two more mills at
that step and jobs no longer queue behind a single machine — throughput at the
constraint roughly triples.

## The result: better, not ideal

Run both at 30 replications and compare the on-time confidence range:

| Shop | On-time (mean) | Range |
|---|---|---|
| `cnc-shop` (1 mill) | ~20% | 10–30% |
| `cnc-shop-3mill` (3 mills) | ~75% | 70–80% |

Buying the mills moves on-time delivery from about a fifth of orders to about
three-quarters — a real gain, but **not** a fix to 100%. The constraint has
moved: with milling no longer the wall, labor, setup, and the downstream
deburr/inspect steps now set the ceiling. That is the honest lesson a twin is
for — capacity spend buys a large but bounded improvement, and it tells you
where the *next* bottleneck will be before you spend.

## What capacity-N does

A location with `capacity: N` is one work center with N identical machines.
Jobs pull whichever machine is free, so up to N run at once. In the rendered
report's material-flow diagram the center is drawn as a **stack of N machine
boxes**, each labelled with its per-job rate.

```yaml
  - name: mill_shaft
    setup_key: grp_mill_shaft
    capacity: 3                 # three parallel CNC mills
    time_model: {kind: rate_based, rate: 0.03}
    machine: mill_shaft_m
    labor_skill: machinist
```

## Run it

```bash
twinflow validate examples/cnc-shop-3mill/model.yaml
twinflow run examples/cnc-shop-3mill/model.yaml --plan examples/cnc-shop-3mill/plan.csv --reps 30
twinflow report <run-id> --out html
```

Use `--reps 30` so the report shows each KPI as a confidence range, not a
single number — that is what makes the before/after comparison trustworthy.
