# Test a Saturday shift

Question: does adding Saturday for the machinists fix the late orders?

## What this does

It adds Saturday to the machinists' week and compares on-time to today.

## Before you start

- You did [Compare to today](compare-to-today.md). Today is 79.7% on time, 3.05 late orders per run.
- The file `scenarios/saturday-shift.json` changes the `days` of the machinists' week. The first value is today's week. The second adds Saturday.

  ```json
  {"labor.pools[machinists].calendar.weekly[0].days": [["mon","tue","wed","thu","fri"], ["mon","tue","wed","thu","fri","sat"]]}
  ```

## Steps

1. Run it.

   ```bash
   twinflow balance examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --sweep examples/showcase/cnc-job-shop/scenarios/saturday-shift.json --reps 20
   ```

   You will see:

   ```
     point        on_time_pct  busy_hours  levers
     ------------ ----------- -----------  ------
     point-0000          79.7       776.9  labor.pools[machinists].calendar.weekly[0].days=['mon', 'tue', 'wed', 'thu', 'fri']
     point-0001          93.7       663.0  labor.pools[machinists].calendar.weekly[0].days=['mon', 'tue', 'wed', 'thu', 'fri', 'sat']
   ```

2. Read it. One more working day a week lifts on-time from 79.7% to 93.7%. Late orders per run (in `summary.json`) fall from 3.05 to 0.95.

## What you get

A Saturday shift for the two machinists does most of what a second mill does (95.7%), with no new machine. The mill still works only when a machinist is there, so more hours of people mean more hours of mill.

To test longer weekdays instead, use a path that ends in `.end`, such as `labor.pools[machinists].calendar.weekly[0].end`, and give times in quotes.

## What it cannot do yet

- Overtime cost is not counted. The table shows on-time and hours only.
- The inspectors keep their own calendar. They are not given the Saturday here.

## If something goes wrong

- **"time data ... does not match format"**: Write times like `"22:00"`.
- **Nothing changes**: Check that the pool name in the path is `machinists`.

## Next

[Test a WIP cap](test-a-wip-cap.md).
