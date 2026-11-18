# Harbor Pump Works: can we ship December's orders?

A pump plant buys motors, housings, impellers and seals, and assembles two pumps. The
motors and impellers come through a West Coast port that has been late this fall. This
folder answers: **which customer orders will ship on time, which are at risk, and does the
buyer's plan B (air freight and two new purchase orders) fix them?**

Open `pump-supply.ipynb`. It tells the story, shows each input file, runs every command,
and ends with the decision. The answers are saved in it.

## What is in this folder

| File | What it is |
|---|---|
| `data/items.csv` | The item master: the two pumps and the parts we buy. |
| `data/suppliers.csv` | Each supplier, and the fewest and most days late it ships. Suppliers in the same risk group are late together. |
| `data/on-hand.csv` | Stock on the shelf this morning. |
| `data/open-purchase-orders.csv` | Parts on order, with the promised dock dates. |
| `data/customer-orders.csv` | Customer orders, due dates, priority and price. |
| `harbor-pumps.twin.yaml` | The twin capsule. It points at the files above, and holds the bill of materials and the assembly days. |
| `data/open-purchase-orders-plan-b.csv` | The buyer's plan B purchase orders. |
| `harbor-pumps-plan-b.twin.yaml` | A copy of the capsule that uses the plan B purchase orders. |

## The answer, in short

- Today, one order can never ship (no small housings or impellers on order) and three
  more are on time in fewer than half of the runs.
- Plan B makes three of them safe and lifts the fourth from 14% to 66%. The rest of that
  risk is the large impellers, which still come through the port.

## Run it yourself

From this folder (if you work from a twinflow source checkout, put `uv run` in front):

```bash
twinflow scenario validate harbor-pumps.twin.yaml
twinflow scenario run harbor-pumps.twin.yaml --out runs/today --summary
twinflow scenario run harbor-pumps-plan-b.twin.yaml --out runs/plan-b --summary
```

`--summary` prints one line per customer order. The full result, with every shortage
and its parts path, is `result.json` in the `--out` folder.

## Good to know

- Each list in the capsule (`suppliers`, `items`, `inventory`, `receipts`, `orders`) can
  be the name of a CSV file next to it. A header like `delay_risk.maximum_days` fills a
  nested field. Extra columns such as `customer` and `unit_price` are kept but not used.
- The dates cover parts and assembly time. The twin does not model the assembly line's
  capacity.
