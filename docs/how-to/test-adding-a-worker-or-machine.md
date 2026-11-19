# Test adding a worker or machine

Question: is a third machinist or a second mill worth it?

## What this does

It runs every mix of 2 or 3 machinists, and 1 or 2 valve-body mills, side by side.

## Before you start

- You did [Compare to today](compare-to-today.md). Today is 2 machinists and 1 mill.
- The sweep file is `sweeps/machinist-or-mill.json`:

  ```json
  {"labor.pools[0].headcount": [2, 3], "locations[mill_VB].capacity": [1, 2]}
  ```

## Steps

1. Run the sweep.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/sweeps/machinist-or-mill.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  labor.pools[0].headcount=2, locations[mill_VB].capacity=1
     point-0001          95.7       888.5  labor.pools[0].headcount=2, locations[mill_VB].capacity=2
     point-0002          80.3       693.1  labor.pools[0].headcount=3, locations[mill_VB].capacity=1
     point-0003          99.7       569.4  labor.pools[0].headcount=3, locations[mill_VB].capacity=2
   ```

2. Read it. Point 0 is today. A third machinist alone gives 80.3%, a gain of 0.6 points. A second mill gives 95.7%. Both give 99.7%.

3. Pick. The second mill is the fix. The third machinist adds about 4 points on top of it. Weigh that against what a machinist costs.

4. Run it again with another plan file, for a harder case. For the surge file, the answer changes: only the pair of both gives 99.1%.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/scenarios/order-influx.csv --sweep examples/showcase/cnc-job-shop/sweeps/machinist-or-mill.json --reps 20
   ```

   ```
     point-0000          64.8     1261.1  labor.pools[0].headcount=2, locations[mill_VB].capacity=1
     point-0001          66.7     1285.5  labor.pools[0].headcount=2, locations[mill_VB].capacity=2
     point-0002          66.7     1294.1  labor.pools[0].headcount=3, locations[mill_VB].capacity=1
     point-0003          99.1     1187.6  labor.pools[0].headcount=3, locations[mill_VB].capacity=2
   ```

## What you get

A side-by-side table. The twin shows the options. You choose. Open the `sweep.html` the command prints for a picture. The `summary.json` next to it also has the late orders per run: 3.05 today, 0.65 with a second mill, 0.05 with both.

## What it cannot do yet

- The table shows on-time percent and busy hours. It does not add the cost of a machinist or a mill.
- A second mill is tested as `capacity: 2` on the mill step.

## If something goes wrong

- **A short error that is one word, such as `error: 'release'`**: A path in the sweep file does not match the shop file. Pools and steps can be picked by number (`[0]`) or by name (`[machinists]`).
- **The sweep is slow**: Each point runs 20 times. Use fewer points.

## Next

[Test a Saturday shift](test-a-saturday-shift.md).
