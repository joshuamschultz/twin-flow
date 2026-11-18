# Ridgeline Precision: will the open orders ship on time?

A CNC job shop makes fittings, valve bodies and brackets to order on two shifts. It has
15 open work orders due over three weeks. This folder answers: **which orders will be
late, which machine holds them up, and does a third machinist or a second mill fix it?**

Open `cnc-job-shop.ipynb`. It tells the story, shows each input file, runs every command,
and ends with the decision. The answers are saved in it.

## What is in this folder

| File | What it is |
|---|---|
| `data/open-orders.csv` | The open work orders from the ERP: order, customer, part, quantity, release date, due date, rush flag. Dates are plain dates. |
| `shop.yaml` | The shop file: machines, people and shifts (with Thanksgiving closed), the routing of each part, cycle and setup times, prices and late fees. |
| `ridgeline.twin.yaml` | The twin capsule. It points at `shop.yaml` and `data/open-orders.csv` and turns on state capture. |
| `sweeps/machinist-or-mill.json` | The two changes to test: 2 or 3 machinists, 1 or 2 valve-body mills. |

## The answer, in short

- Four valve-body orders are at risk. Fittings and brackets ship on time.
- The valve-body mill is the bottleneck: busy 93% of its on-shift hours.
- A third machinist barely helps (79.7% to 80.3% on time). A second valve-body mill
  lifts on-time to 95.7%.

## Run it yourself

From this folder (if you work from a twinflow source checkout, put `uv run` in front):

```bash
twinflow validate shop.yaml
twinflow run shop.yaml --plan data/open-orders.csv --reps 20 --orders
twinflow balance shop.yaml --plan data/open-orders.csv --sweep sweeps/machinist-or-mill.json --reps 20
twinflow scenario run ridgeline.twin.yaml --out runs/capture --summary
twinflow states runs/capture --view full --top 5
twinflow markov runs/capture --view full --top 4
```

Each `run` prints a folder under `runs/` with a `report.html` to open in a web browser.

## Good to know

- `start_date` and `due_date` can be plain dates because the shop file has a work
  calendar. Its `origin` (Monday 6 am) is time zero. A due date with no time means "by
  the end of that day".
- Steps that share a machine (the saw, the inspection station) each name their own
  `setup_key`, one per part, so a switch between parts is a setup change. The setup time
  of each job is the `load` of its time model.
