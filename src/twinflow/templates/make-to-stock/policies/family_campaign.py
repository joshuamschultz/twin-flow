"""Stay with one family until it is stocked up, then move on.

Switching to another product in the same family is cheap. Switching to a
new family costs a lot. So the press stays in its current family until
every product there has `target_days` days of stock. Then it moves to the
family of the product that will run out first.
"""

import math

NAME = "family_campaign"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"target_days": [6, 8, 12, 16, 20]}


def decide(state, target_days=8):
    # Find the products that are below their stock goal.
    short = []
    for product in state.products:
        # Do not stock more than the press can hold, or more than the days left can sell.
        days = min(target_days, state.days_left)
        goal = min(state.max_storage[product], math.ceil(days * state.demand_rate[product]))
        if state.stock[product] < goal:
            short.append(product)

    # Everything is stocked up: rest the press.
    if not short:
        return "idle"

    # Prefer products in the family the press is already set up for.
    current_family = state.family[state.setup]
    same_family = [p for p in short if state.family[p] == current_family]
    choices = same_family or short

    # Among those, run the one that will run out first.
    neediest = choices[0]
    for product in choices:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product
    return neediest
