# Test a missing person

Question: what happens if a machinist is out?

## What this does

It tests two cases. One is a lost machinist for the whole test. The other is a chance that each machinist is out.

## Before you start

- You did [Compare to today](compare-to-today.md). Today is 79.7% on time, 3.05 late orders per run.
- The files are `scenarios/machinist-headcount.json` and `scenarios/machinist-absence.json`.

## Steps

1. Lose one machinist for the whole test. The file is `{"labor.pools[machinists].headcount": [1, 2]}`.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/machinist-headcount.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000           8.7       817.3  labor.pools[machinists].headcount=1
     point-0001          79.7       776.9  labor.pools[machinists].headcount=2
   ```

2. Test a chance of absence. `absence_rate` is the chance that each person is out. The file has 0, 0.25 and 0.5.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/machinist-absence.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  labor.pools[machinists].absence_rate=0
     point-0001          41.7       795.6  labor.pools[machinists].absence_rate=0.25
     point-0002          30.7       804.0  labor.pools[machinists].absence_rate=0.5
   ```

3. Read it against today. With one machinist the shop ships 8.7% on time, not 79.7%. Even a 25% chance of absence cuts it to 41.7%. Late orders per run (in `summary.json`) go from 3.05 to 13.7 for one machinist, and to 8.75 for the 25% case.

## What you get

The shop has no spare machinist. Two people are the least it can run on. A third machinist or a cross-trained person is worth testing. See [Test adding a worker or machine](test-adding-a-worker-or-machine.md).

## What it cannot do yet

- Absence is drawn once per run, not per shift. A person is out for the whole test or not at all (`src/twinflow/plan/driver.py:617`). Real absences are one day here and there.
- A pool never drops below 1 person, so a full-crew absence cannot be tested.
- You cannot name who is out, or choose the days.

## If something goes wrong

- **The pool name is not found**: The sweep path uses the pool `name`, here `machinists`. Check the spelling in `shop.yaml`.

## Next

[Test a rush order](test-a-rush-order.md).
