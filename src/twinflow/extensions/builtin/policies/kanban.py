"""Kanban cards: refill a product when it drops under a share of its target.

Each product has a target stock of `target_days` days. When its stock falls
under `trigger_share` of the target, its card is released. Run the released
product that will run out first. No card released: rest the press.
"""

import math

NAME = "kanban"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"target_days": [6.0, 10.0, 14.0, 20.0], "trigger_share": [0.3, 0.5, 0.7]}


def decide(state, target_days=10.0, trigger_share=0.5):
    # Near the end of the run, do not aim for more than the days left will sell.
    days = min(target_days, max(0, state.days_left))
    released = []
    for product in state.products:
        target = min(state.max_storage[product], math.ceil(days * state.demand_rate[product]))
        if state.stock[product] < trigger_share * target:
            released.append(product)

    if not released:
        return "idle"

    # Run the released product with the fewest days of stock.
    neediest = released[0]
    for product in released:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product
    return neediest
