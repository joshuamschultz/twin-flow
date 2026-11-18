"""Tomorrow's sales look like a real day from the last 13 weeks.

This is a demand file. It reads `data/sales-history.csv` (one row per day and bin,
exported from the ERP). For each simulated day it picks one real past day at random
and uses that day's sales for every bin. Busy Mondays and slow Sundays stay in the mix,
and so do the days when several bins sold well together.
"""

import csv
from pathlib import Path

NAME = "sales_history"

# The sales file, next to this folder. <- CHANGE: point at a newer export here.
HISTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "sales-history.csv"


def _read_history():
    """Each past day as {bin: units sold}, oldest day first."""
    days = {}
    with HISTORY_FILE.open(newline="") as handle:
        for row in csv.DictReader(handle):
            day = days.setdefault(row["date"], {})
            day[row["product"]] = int(row["units_sold"])
    return [days[date] for date in sorted(days)]


HISTORY = _read_history()


def demand(period, rng, model):
    # KEY LINE: pick one real past day at random. The race gives every rule the same picks.
    day = HISTORY[int(rng.integers(len(HISTORY)))]
    sales = {}
    for product in model.products:
        if product in day:
            sales[product] = day[product]
        else:
            # A new bin with no history yet: draw around its average from products.csv.
            sales[product] = int(rng.poisson(model.demand_rate[product]))
    return sales
