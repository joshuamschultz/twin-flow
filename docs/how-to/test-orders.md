# Test orders

This guide runs a shop's work orders and reads the results. You check finish dates and the on-time percent.

## What this does

It runs the 10 work orders of the CNC shop example. Then it shows when each order finished, and how many finished on time.

## Before you start

- twinflow is installed.
- The example is in `examples/cnc-shop/` of the twinflow folder.
- Run the commands from a work folder. Results are saved in a `runs/` folder there.
- Know the plan file. Each row is one work order. Its columns are `work_order_id`, `part`, `qty`, `start_date` and `due_date`. Dates and times are in seconds from the start. For example, 3600 seconds is one hour.

  ```
  work_order_id,part,qty,start_date,due_date
  wo-1,shaft,15,0,1500
  wo-2,bracket,20,100,1600
  ```

## Steps

1. Check the shop file. Use `model.yaml`, not the `.twin.yaml` file.

   ```bash
   twinflow validate examples/cnc-shop/model.yaml
   ```

   No message means the file passed.

2. Run the 10 orders once.

   ```bash
   twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 1
   ```

   You will see something like this. Your run ID will differ:

   ```
   run run-0-0-d3aedf55
     orders:   10/10 complete  |  on-time 9 (90%)  |  late 1
     makespan: 1.0 h
     dir:    runs/run-0-0-d3aedf55
     report: runs/run-0-0-d3aedf55/report.html
   ```

3. Run the orders 30 times. One run can be lucky or unlucky, so 30 runs give a fairer answer.

   ```bash
   twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30
   ```

   The orders line now ends with `(rep 1 of 30)`. That line describes the first run only. The report uses all 30 runs.

4. Confirm the report. Use the run ID from step 2 or 3.

   ```bash
   twinflow report run-0-0-d3aedf55 --out html
   ```

   When the report is there, this command prints nothing. Then open `runs/run-0-0-d3aedf55/report.html` in a web browser.

5. Read the Overview tab. The headline shows the on-time percent over all runs, for example "On-time 87% range 83-91% over 30 runs". It also shows how many orders finished and the busiest machine.

6. Read the Detail tab. Find the table "Completion & lateness by order".

   - `Done (s)` is the finish time, in seconds from the start.
   - `Late` is negative when an order finishes early, and positive when it finishes late.

   For example, order `wo-9` is due at 3500 seconds. In the first run it finished at 3519 seconds, which is 19 seconds late.

## What you get

- A run folder with the report, the summary numbers and the raw data.
- For the 30-run example: about 87% of orders finish on time, with a range of 83% to 91%.
- On average, only `wo-9` finishes late, by about one minute.
- The report names `mill_shaft` as the busiest machine, at 99% busy. This is the machine to watch first.

## If something goes wrong

- **"No such file or directory"** or a similar message about the plan: The path to `plan.csv` is wrong. Check the folder and the file name.
- **"unknown part 'gear' at row 2"**: A `part` value in the plan does not match a part in the shop file. In this shop, the parts are `shaft` and `bracket`.
- **A number in the shop file is a word**: `validate` may still pass it. The run then stops with a message such as "could not convert string to float: 'slow'". Find the word and change it to a number.
- **You pass the `.twin.yaml` file to `validate` or `run`**: You see an error such as "'part_types'". Use `model.yaml` with these commands.
- **"--reps must be a positive integer"**: Use a number of 1 or more.

## Next

[Write a policy](write-a-policy.md): write your own rule for what to make next.
