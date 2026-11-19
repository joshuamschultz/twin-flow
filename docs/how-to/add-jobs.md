# Add jobs

This guide writes the orders file, and runs it.

## What this does

It shows the columns of an orders file, and how to read one line per order after a run.

## Before you start

- You did [Model your floor](model-your-floor.md).
- The orders file is `examples/showcase/cnc-job-shop/data/open-orders.csv`. Each row is one work order.

## Steps

1. Know the columns.

   ```
   work_order_id,customer,part,qty,start_date,due_date,priority
   WO-26102,Summit Valve,VB-3107,40,2026-11-16,2026-11-18,0
   ```

   - `part` must be a part in `routing`.
   - `start_date` is when the order is released.
   - `due_date` with no time means the end of that day.
   - `priority` is optional. 0 is normal. See [Test a rush order](test-a-rush-order.md).

2. Add a row for each new order. Keep each `work_order_id` new.

3. Run the orders 20 times.

   ```bash
   twinflow run examples/showcase/cnc-job-shop/shop.yaml --plan examples/showcase/cnc-job-shop/data/open-orders.csv --reps 20 --orders
   ```

4. Read one line per order. Your run ID will differ, and some lines here are left out.

   ```
   run 2026-11-19T00-56-14Z-4af4bad4
     orders:   15/15 complete  |  on-time 12 (80%)  |  late 3 (rep 1 of 20)
   Order     Part      Qty  Due               Done (typical)    Done (bad case)   Late in
   WO-26105  VB-3107   50   Fri Nov 20 24:00  Fri Nov 20 18:45  Mon Nov 23 12:04  8 of 20
   WO-26107  VB-3107   30   Mon Nov 23 24:00  Tue Nov 24 11:30  Tue Nov 24 19:26  16 of 20
   WO-26110  VB-3107   45   Wed Nov 25 24:00  Mon Nov 30 17:58  Tue Dec 01 10:23  20 of 20
   ```

   `Late in` counts the runs, out of 20, where the order was late. The top line is the first run only.

## What you get

- A finish date for each order, for a typical run and for a bad case.
- The orders that are at risk. Here WO-26110 is late in all 20 runs.

## If something goes wrong

- **"unknown part 'VB-9999' at row 3"**: The `part` is not in the shop file. Use `FTG-2201`, `VB-3107` or `MB-4410`.
- **"due_date 'soon' is not a date"**: Write it like `2026-11-20` or `2026-11-20 15:30`.

## Next

[Test orders](test-orders.md): read the due-date results of a smaller shop in more detail.
