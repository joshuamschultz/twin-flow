# Test a rush order

![A queue of crates in front of a machine, with one crate lifted to the front](../images/headers/how-to-test-a-rush-order.webp)

This guide answers one question. Can we fit one rush order in, and who pays for it?

## What this does

It adds a rush valve-body order. It runs the shop with the rush flag on, and with the flag off, so you see what the flag buys.

## Before you start

- You did [Compare to today](compare-to-today.md). Today is 79.7% on time.
- The files are `scenarios/rush-order.csv` and `scenarios/rush-order-normal.csv`. Each is the open orders plus one row, `WO-26116`. The rows differ only in `priority`: 5 in the first, 0 in the second.

  ```
  WO-26116,Delta Pumps,VB-3107,20,2026-11-18,2026-11-24,5
  ```

## Steps

1. Run the rush order with priority 5. A higher `priority` goes first at every machine.

   ```bash
   twinflow run examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/scenarios/rush-order.csv --reps 20 --orders
   ```

   Your run ID will differ. Some lines are left out:

   ```
   Order     Part      Qty  Due               Done (typical)    Done (bad case)   Late in
   WO-26107  VB-3107   30   Mon Nov 23 24:00  Wed Nov 25 11:45  Wed Nov 25 21:22  20 of 20
   WO-26116  VB-3107   20   Tue Nov 24 24:00  Mon Nov 23 16:37  Tue Nov 24 09:23  0 of 20
   ```

2. Run the same order with priority 0.

   ```bash
   twinflow run examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/scenarios/rush-order-normal.csv --reps 20 --orders
   ```

   ```
   Order     Part      Qty  Due               Done (typical)    Done (bad case)   Late in
   WO-26107  VB-3107   30   Mon Nov 23 24:00  Tue Nov 24 09:15  Tue Nov 24 19:44  17 of 20
   WO-26116  VB-3107   20   Tue Nov 24 24:00  Wed Nov 25 12:03  Wed Nov 25 19:28  17 of 20
   ```

3. Read it. With the flag, `WO-26116` is late in 0 of 20 runs. Without it, 17 of 20. The cost: `WO-26107` slips from Tue Nov 24 09:15 to Wed Nov 25 11:45, and goes from late in 17 runs to 20.

## What you get

See where this test sits next to the others in the [bad-day results](compare-to-today.md#bad-day-results).

Mean on-time percent, over 20 runs (from `balance`, see [Compare to today](compare-to-today.md)):

- Today, no new order: 79.7%.
- Rush order flagged: 73.8%.
- Same order not flagged: 69.1%.

The flag saves the rush order and pushes one other order later. A rush on a full mill always costs someone.

## What it cannot do yet

- A rush order waits for the job on the machine to end. It never stops a job in the middle. Here the order cannot make Friday, so the test uses Tuesday.
- `priority` is a number. There is no cost of lateness per customer in the ordering.

## If something goes wrong

- **Nothing changes**: Check that `priority` is in the header row, and that the number is above the other rows.
- **A rush order is still late**: The mill is full before it arrives. Move the due date, or test a second mill.

## Next

[Test an order surge](test-an-order-surge.md).
