# Change the dispatch rule

Question: does a different "what do we run next" rule at the mill help?

## What this does

It tries three rules at the valve-body mill. EDD means earliest due date first. FIFO means first in, first out. SPT means shortest job first.

## Before you start

- You did [Compare to today](compare-to-today.md). Today uses `edd` at the mill, 79.7% on time.
- The file `scenarios/dispatch-rule.json` is `{"locations[mill_VB].dispatch": ["edd", "fifo", "spt"]}`.

## Steps

1. Run it.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/dispatch-rule.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  locations[mill_VB].dispatch=edd
     point-0001          79.7       776.9  locations[mill_VB].dispatch=fifo
     point-0002          81.3       782.4  locations[mill_VB].dispatch=spt
   ```

2. Read it. EDD and FIFO give the same result here. SPT gives 81.3%, a gain of 1.6 points. Late orders per run go from 3.05 to 2.8 (in `summary.json`).

## What you get

A rule change is a small lever here. The mill has too much work, and no order rule makes more hours. SPT runs short jobs first, so a big order can wait longer. Read the order table before you adopt it.

## What it cannot do yet

- `twinflow optimize` cannot search rule names, only whole numbers. Use `balance` for rules.
- A rule applies to one step. You set it step by step.

## If something goes wrong

- **"unknown dispatch rule 'zzz'; expected one of ['critical_ratio', 'edd', 'fifo', 'spt']"**: Use one of the four names.

## Next

[Optimize your floor](optimize-your-floor.md): let the twin search for the best change.
