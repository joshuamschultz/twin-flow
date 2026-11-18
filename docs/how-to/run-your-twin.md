# Run your twin

This guide runs a plant many times and saves the results. It also shows how to turn on state capture for a job shop, and how to read what it recorded.

## What this does

It runs the plant on random demand, many times over. Then it saves the results in a folder you can read.

## Before you start

- You have checked your plant file. See [check your twin](check-your-twin.md).
- Pick a work folder. Results are saved in a `runs/` folder inside the folder where you run the command, unless you name a folder with `--out`.

## Steps

### Part A: Run the molding press

1. Run the example press 50 times. Each run covers 60 days. The `--seed` number makes the random demand the same every time you run it.

   ```bash
   twinflow scenario run examples/molding-press/molding-press.twin.yaml --reps 50 --seed 2024 --out press-out
   ```

   The command prints a long summary in JSON form. Look for `"mean_profit"` under `"metrics"`. For this seed it is about 44,100 dollars for 60 days.

2. Open the `press-out` folder. It holds:

   - `result.json`: the summary you saw on screen.
   - `scenario.json`: the plant facts used for the run.
   - One folder for each run, named `rep-0000`, `rep-0001` and so on. Each one holds `moves.parquet` and `states.parquet`. These record what the plant did each day.

### Part B: Turn on state capture for a job shop

A make-to-stock plant records its days on its own. A job shop does not. You turn recording on in the plant file.

1. Make a job shop folder from the starter.

   ```bash
   twinflow new my-shop --template job-shop
   ```

2. Open `my-shop/my-shop.twin.yaml` and find the `experiment:` section. It looks like this:

   ```yaml
   experiment:
     id: cnc-shop-example
     replications: 1
     seed: 0
     state_capture:       # record how the shop moves, so `twinflow states` can read it
       every_hours: 0.1
   ```

3. Know what `every_hours` does. It must be shorter than one run. The starter shop runs about one hour of simulated time, so the starter uses `0.1`, which is 6 minutes. A value like 8 catches almost nothing: 3 moves for 3 runs. Leave it at `0.1` for now.

4. Run the shop 3 times and save the recording.

   ```bash
   twinflow scenario run my-shop/my-shop.twin.yaml --reps 3 --out shop-out
   ```

### Part C: Read the states

Read what the saved runs recorded. The press first:

```bash
twinflow states press-out
```

For the press, you see the family view, with money by part at the bottom. This sample is from a 2-run test:

```
Moves found in press-out
Runs found: 2
Episodes: 2
Moves: 120
Time span: 0 to 60
Unique states: 121
Top states (view family), share of moves:
   23.3%  grey:low black:low natural:low | grey
   13.3%  grey:low black:low natural:low | natural
Ledger by part (all moves):
  revenue        515,564.80
  production     293,578.80
  setup          104,600.00
  holding         20,245.52
  lost_sales      10,396.68
  total           86,743.80
```

Then the job shop:

```bash
twinflow states shop-out
```

For the job shop, the view is `full`, because the shop file has no product families. Each state is a snapshot of the shop: the raw material on hand, and what each work center is set up for. The most common states are listed first, with the money by part at the bottom. Your numbers will differ.

## What you get

- A folder with a summary (`result.json`) and one folder per run.
- For a job shop with state capture, a list of the most common states and the money by part.

## If something goes wrong

- **"No moves found ... Turn on state capture"**: A job shop run did not record anything. Add `state_capture` to the `experiment:` section, as in Part B, and run again.
- **"Experiment output directory must be new or empty"**: The `--out` folder already exists and has files in it. Pick a new folder name. Each run needs its own folder.
- **"does not exist or is not a folder"**: The folder name is wrong, or the run did not finish. Check the name you gave to `--out`.
- **The job shop shows one state for every move**: The `every_hours` value is too big for the run length. Lower it, as in Part B, step 3.

## Next

[Test orders](test-orders.md): run a shop's orders and read the due-date results.
