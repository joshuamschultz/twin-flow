"""Stay in one color until it is stocked up, but never let a key bin run out.

This is a candidate policy: a new rule to test against the ones we use. It starts
from `family_campaign`. One change: if a key bin (an A item) is about to run out,
the press switches to it first, even when that means a color change.
"""

import math

NAME = "campaign_guard_key_items"

# Values the race may try. <- CHANGE: the stock goal in days, and the alarm level.
SETTINGS = {"target_days": [8, 12, 16], "alarm_days": [1.0, 2.0]}


def decide(state, target_days=12, alarm_days=2.0):
    # KEY LINE: an A item with less than `alarm_days` of stock goes first, color change or not.
    urgent = [
        p for p in state.products if state.abc[p] == "A" and state.days_of_cover[p] < alarm_days
    ]
    if urgent:
        return min(urgent, key=lambda p: state.days_of_cover[p])

    # Otherwise, the family campaign: find the bins below their stock goal.
    short = []
    for product in state.products:
        days = min(target_days, state.days_left)
        goal = min(state.max_storage[product], math.ceil(days * state.demand_rate[product]))
        if state.stock[product] < goal:
            short.append(product)
    if not short:
        return "idle"

    # Stay in the color the press is set up for, when that color still needs stock.
    current_family = state.family[state.setup]
    same_family = [p for p in short if state.family[p] == current_family]
    choices = same_family or short
    return min(choices, key=lambda p: state.days_of_cover[p])
