# Troubleshooting

Symptom, cause, fix. Most problems are caught before a run by `twinflow validate`; run it
first.

## The model won't validate

```bash
twinflow validate model.yaml
```

It prints one line per problem, as `path: message`. Common ones:

| Message mentions | Cause | Fix |
|---|---|---|
| unknown part / thing | a `routing`, `consumes`, or `emits` names a part not in `part_types` | add the part to `part_types`, or fix the typo |
| uom mismatch | a `consumes`/`emits`/`stock` uses a different unit than the part's declared `uom` | make the units match; a part has exactly one uom |
| branches must sum to 1.0 | a `quality_gate`'s branch probabilities don't total 1 | adjust the `prob` values |
| unknown supplier / supplier cycle | a stock's `supplier` names a missing stock, or two stocks supply each other | point `supplier` at a real upstream stock; break the cycle |
| routing step not a location | a `routing` step names something that isn't a declared location | fix the step name |

Validation inspects the raw config and never runs the sim, so it is fast and safe to run
often.

## The run never finishes (deadlock)

A run that hangs is almost always a material or routing dead end:

- **A stock starts empty with no way to refill.** A location consumes from a `material`
  stock that has no `reorder_point`/`refill_to` and no initial `initial`, so it waits
  forever. Fix: give the stock an `initial` level or a reorder policy.
- **A routing gap.** An operation's output has nowhere to go and the next step never gets
  its input. Fix: check `routing` lists the steps in the right order and every consumed
  `thing` is produced upstream (or supplied by a stock/plan release).
- **A WIP cap that is too low.** `release: {policy: wip_cap, wip_cap: N}` with a tiny `N`
  can stall if a single order needs more than `N` in flight. Fix: raise `wip_cap`.

Reproduce with `--reps 1` for the fastest signal, and read `twinflow validate` output
first.

## KPIs come back empty or zero

- **`on_time_pct` is 0 and every order shows no completion.** Nothing finished within the
  run horizon. The floor is too slow for the plan, or a step is starved. Look at
  `utilization_by_cell` and the wait breakdown (starved / blocked / material) to find the
  choke, then add capacity, staff, or material.
- **`inventory` is empty.** The model declares no `stocks`. That is expected; the inventory
  view only appears for floors with stocks.
- **`setup_hours` is 0.** No location declares `changeover_seconds`, or every job stays in
  one setup group. That is correct, not a bug.

## The optimizer seems stuck or slow

- **Budget vs space.** `budget` is the number of real (simulated) evaluations. A big grid
  with a small budget only explores a prefix; a stochastic optimizer on a tiny space just
  re-checks cached points until it stops. Every optimizer is guaranteed to terminate.
- **Reps drive the cost.** Each evaluation runs `reps` replications. Lower `reps` while
  exploring, then confirm the winner at higher `reps`.
- **Levers must be integers for `IntRange`.** The RL env and `--lever` ranges are integer
  knobs (staffing, capacity, reorder points). A continuous knob is not yet a lever.

## Runs are slow or leave many folders

- Each replication runs in its own process (`multiprocessing`), and each writes a
  `runs/<id>/` folder. The CLI cleans up after itself; ad-hoc Python scripts do not.
- If you run the library directly in a loop, wrap it so the working directory is a scratch
  dir, or expect `runs/` to accumulate. They are safe to delete.
- On heavy machines, a killed test/optimize run can leave orphaned worker processes. Clear
  them with `pkill -9 -f "python -m pytest"` (or the matching command) before re-running.

## Multiprocessing errors ("can't pickle", "No module named __main__")

- Run scripts as real files with an `if __name__ == "__main__":` guard, not piped into
  `python -` from stdin. The worker processes re-import the main module by path, which a
  heredoc/stdin script does not have.

## Results aren't reproducible

They should be, exactly. If two runs differ:

- Confirm you pass the same `base_seed` (and, for a sweep/optimize, that the same lever
  values map to the same point).
- A new stochastic feature must draw from its own seeded RNG stream; if you added one and
  results drift, that stream is the suspect.
- The run stamp (`run_meta.json`) records the model hash, plan hash, seed, and dependency
  set. Two runs with the same stamp must produce the same numbers.

## The API or front end won't connect

- Start the API first: `twinflow serve --port 8000` (needs `pip install -e ".[api]"`).
- The front end proxies `/api` to `http://localhost:8000`; both must be running. See
  [frontend.md](frontend.md).
- A `model` in an API request must name a discovered example (a folder under the models
  root with a `model.yaml` and `plan.csv`), not a file path.

## Still stuck

- Re-read the relevant reference: [modeling.md](modeling.md) for config, [data.md](data.md)
  for the plan/event formats, [running.md](running.md) for how a run works.
- Reproduce with the smallest model and `--reps 1`, then share the `validate` output and the
  `run_meta.json` stamp.
