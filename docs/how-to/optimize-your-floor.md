# Optimize your floor

![A small factory floor with three control sliders beside it and a hill with one peak above it](../images/headers/how-to-optimize-your-floor.webp)

This guide lets the twin search for the best number of machinists and mills.

## What this does

`twinflow optimize` tries settings and scores them. It prints the best one with its range. The twin scores the options. You decide.

## Before you start

- You did [Find your bottleneck](find-your-bottleneck.md).
- A lever is a setting you may change. Here: the machinists in pool 0 (1 to 4) and the capacity of the valve-body mill (1 to 2). Levers are whole numbers.

## Steps

1. Run the search. `grid` tries every mix. There are 8, so the budget is 8.

   ```bash
   twinflow optimize examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --lever "labor.pools[0].headcount:1:4" --lever "locations[mill_VB].capacity:1:2" --objective on_time_pct --optimizer grid --budget 8 --reps 12
   ```

   You will see this. Your run ID will differ:

   ```
   objective on_time_pct (max) -> best score 100.00
     levers:   {'labor.pools[0].headcount': 3, 'locations[mill_VB].capacity': 2}
     on-time%: 100.0  (band 100.0-100.0, 12 reps)
     evals:    8 of budget 8
     (the twin scores the options; the decision stays yours)
   ```

2. Read the winner. It is 3 machinists and 2 mills, at 100% on time.

3. Read the whole table in the `summary.json` the command prints (or open `sweep.html`). On-time percent for each mix, 12 runs:

   ```
   machinists  mills  on-time
   1           1        9.4
   1           2        9.4
   2           1       77.8
   2           2       95.6
   3           1       80.6
   3           2      100.0
   4           1       80.6
   4           2      100.0
   ```

4. Pick the change. The search only maximizes on-time. It does not weigh cost. Two mills with 2 machinists gets 95.6%. The third machinist adds 4.4 points. Four machinists add nothing.

## What you get

- The best mix, with a range.
- A table to weigh the next step against its cost.

## What it cannot do yet

- It ranges over whole numbers only. For rule names use [Change the dispatch rule](change-dispatch-rule.md).
- The default objective ignores cost.

## If something goes wrong

- **Numbers differ a little from the sweep guides**: This guide uses 12 runs, not 20.

## Next

[Write a policy](write-a-policy.md): the next track, tune the rules. Start there when you want a better "what next" rule.
