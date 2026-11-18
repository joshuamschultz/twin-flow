# Lakeshore Molded Products: what should press 3 make next?

A plastics plant makes 12 storage bins on one press, in 3 colors, to stock. Each day the
planner picks one bin to run. A color change costs $1,200 and half a day. This folder
answers: **which rule for "what do I make next" earns the most, and by how much?**

Open `plastics-molding.ipynb`. It tells the story, shows each input file, runs every
command, and ends with the decision. The answers are saved in it, so you can read it
without running anything.

## What is in this folder

| File | What it is |
|---|---|
| `data/products.csv` | The item master: one row per bin (color, A/B/C class, average daily sales, units per day, price, cost, storage limit). |
| `data/sales-history.csv` | Units sold per bin per day for the last 13 weeks, as the ERP exports it. |
| `lakeshore.twin.yaml` | The twin capsule. It points at the two files above, and holds the changeover costs, the money rules and the run settings. |
| `policies/lowest_stock_first.py` | Today's rule: run the bin that runs out first. |
| `policies/family_campaign.py` | Stay in one color until it is stocked up. |
| `candidates/campaign_guard_key_items.py` | A new idea to test: the color campaign, but a key bin never runs out. |
| `rewards/protect_key_items.py` | How the race scores a day: dollars less the big customer's fine for each key bin short. |
| `demand/sales_history.py` | Where tomorrow's sales come from: a random real day from the sales history. |

## The answer, in short

- Today's rule makes about $39,100 per 60 days. It changes color almost every day.
- Staying in one color makes about $63,900. The new rule with the key-bin guard makes
  about $66,300. That is about $165,000 a year more than today.

## Run it yourself

From this folder (if you work from a twinflow source checkout, put `uv run` in front):

```bash
twinflow scenario validate lakeshore.twin.yaml
twinflow extensions check .
twinflow policies race . --out runs/race
twinflow states runs/race/policies/family_campaign --view simple --top 5
twinflow markov runs/race/policies/family_campaign --view simple --top 6 --min-prob 0.1
twinflow improve . --candidate candidates/campaign_guard_key_items.py --out runs/improve
```

Open `runs/race/race.html` in a web browser for the full race report.

`improve` copies the candidate into `policies/` when it wins. To start over, delete
`policies/campaign_guard_key_items.py` and the `runs/` folder.

## Good to know

- The capsule line `products: data/products.csv` reads the item master from its own file.
  The columns must be the ones above. A typo gets a plain message naming the file and row.
- `twinflow scenario run` uses simple random sales around each bin's average. The race
  uses `demand/sales_history.py`, so it replays your real days.
- Analysts who want charts and the Python API can read
  [`../../molding-press/notebooks/analyst/`](../../molding-press/notebooks/analyst/).
