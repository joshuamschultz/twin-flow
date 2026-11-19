# Test a WIP cap

![A gate holding a line of crates back before a machining area](../images/headers/how-to-test-a-wip-cap.webp)

This guide answers one question. Do we ship more on time if we let in fewer orders at once?

## What this does

It holds new orders back until the floor has room. WIP means work in process, the orders on the floor now.

## Before you start

- You did [Compare to today](compare-to-today.md). Today is 79.7% on time, 3.05 late orders per run.
- The file `scenarios/wip-cap.json` has three points: no cap, a cap of 6 and a cap of 3.

  ```json
  {"release": [{"policy": "plan"},
               {"policy": "wip_cap", "wip_cap": 6},
               {"policy": "wip_cap", "wip_cap": 3}]}
  ```

## Steps

1. Run it.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/wip-cap.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  release={'policy': 'plan'}
     point-0001          79.7       776.9  release={'policy': 'wip_cap', 'wip_cap': 6}
     point-0002          83.3       863.7  release={'policy': 'wip_cap', 'wip_cap': 3}
   ```

2. Read it. A cap of 6 changes nothing here. A cap of 3 lifts on-time by 3.6 points, to 83.3%. Late orders per run go from 3.05 to 2.5 (in `summary.json`).

## What you get

See where this test sits next to the others in the [bad-day results](compare-to-today.md#bad-day-results).

A small gain here. The mill is the limit, not the crowd on the floor. Use a cap when a shop is flooded with orders that all start on the same day.

## What it cannot do yet

- The shop file has no `release` setting, so the path `release.wip_cap` fails with `error: 'release'`. Give the whole `release` value, as above.
- The cap counts orders, not hours of work.

## If something goes wrong

- **A very low cap can stall the run**: Raise the cap. See [Troubleshooting](../troubleshooting.md).

## Next

[Change the dispatch rule](change-dispatch-rule.md).
