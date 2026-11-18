"""Run the product whose missing stock is worth the most money.

Each product has a stock goal of `target_days` days. The gap to the goal,
times the sale price, is the money at risk. Run the product with the biggest
gap. No gap anywhere: rest the press.
"""

import math

NAME = "dollar_shortfall"

# Values the race can try, to find the best one for this plant.
SETTINGS = {
    "target_days": [3.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0, 24.0, 28.0],
}


def decide(state, target_days=8.0):
    # Near the end of the run, do not aim for more than the days left will sell.
    days = min(target_days, max(0, state.days_left))

    # Find the biggest gap in dollars (on a tie, the first product).
    worst, worst_gap = None, 0
    for product in state.products:
        goal = min(state.max_storage[product], math.ceil(days * state.demand_rate[product]))
        gap = max(0, goal - state.stock[product]) * state.price[product]
        if worst is None or gap > worst_gap:
            worst, worst_gap = product, gap

    if worst_gap > 0:
        return worst
    return "idle"
