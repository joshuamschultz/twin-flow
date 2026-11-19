# Compare to today

This guide shows how to measure "today" once, then compare every what-if to it fairly.

## What this does

twinflow has no single command that puts a changed plan next to today's plan. You run twice and compare. This guide shows how to make the two runs fair, and gives the baseline numbers that the other guides use.

## Before you start

- You did [Add jobs](add-jobs.md).
- The scenario files are in `examples/showcase/cnc-job-shop/scenarios/`.

## Steps

1. Know why the runs match. `twinflow run` and `twinflow balance` have no `--seed` flag. They always use seed 0, so the same files give the same numbers every time. A change in the numbers comes from your change. (`twinflow optimize` has a `--seed` flag. It only sets the search's own luck.)

2. Use 20 runs (`--reps 20`) every time, for today and for each what-if.

3. Get today's headline numbers. `balance` with a one-point file prints the mean on-time percent over all runs. The file `scenarios/today.json` has one point, 2 machinists, which is how the shop is today.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/today.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  labor.pools[machinists].headcount=2
   ```

4. Get the late orders per run. Open `summary.json` in the folder the command printed. Today it says `"late_orders_per_run": 3.05`.

5. Run the what-if the same way. Change one thing only: the plan file, or one value in a sweep file. Keep the `--reps` the same.

6. Compare on three numbers: on-time percent, late orders per run, and the busiest machine from `run --orders`.

## What you get

Today, the CNC shop has:

- On-time: 79.7%.
- Late orders: 3.05 per run.
- Busiest machine: `mill_VB`, 93% of its on-shift time.

The other test guides compare to these numbers.

## What it cannot do yet

- There is no command that runs two plans and prints the difference. You run twice.
- You cannot choose the seed of `run` or `balance`.

## If something goes wrong

- **Your numbers differ from this page**: Check that you used `--reps 20`, and that no file changed.
- **A one-point sweep looks odd**: It is a trick to get the means. The lever is set to today's value, so it changes nothing.

## Next

[Test a down machine](test-a-down-machine.md).
