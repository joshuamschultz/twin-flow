# Job shop starter

A small CNC shop makes shafts and brackets. Each part goes through four steps:
saw, mill, deburr and inspect. Ten work orders come in over the horizon.
The question: when a machine is free, which waiting job goes next?

## What is in this folder

- `job-shop.twin.yaml` is the shop: machines, routings, orders and the money.
  It is the `cnc-shop` example with state capture and money turned on.
- `policies/busy_edd.py` is a dispatch rule. It picks the job due soonest when work piles up.

## Try it

Run the shop three times and keep the moves:

```bash
twinflow scenario run job-shop.twin.yaml --reps 3 --out out
```

Look at how the shop moved from state to state:

```bash
twinflow states out
twinflow markov out
```

## Good to know

- The rule in `policies/` is used from Python with `twinflow.extensions.dispatch.des_runtime`.
  `scenario run` uses the built-in rule named in the capsule.
- `twinflow policies race` is for make-to-stock plants and custom engines, not for a job shop.
