"""The (s, S) rule: refill a product when it drops to s, up to S.

The refill point s is `safety_days` days of sales. The refill goal S is s plus
one economic run size (the run that balances switch cost against holding
cost), scaled by `eoq_scale`. Once the press starts a product it keeps going
until the product is close to S.
"""

import math

NAME = "s_big_s"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"safety_days": [3.0, 5.0, 7.0, 10.0], "eoq_scale": [0.25, 0.5, 1.0]}


def run_size(state, product):
    # The economic run size: more units when switches cost more, fewer when
    # holding stock costs more.
    switch_cost = (
        state.changeover["within_family"]["cost"] + state.changeover["across_family"]["cost"]
    ) / 2
    rate = state.demand_rate[product]
    return math.sqrt(2 * rate * switch_cost / state.holding_cost[product])


def decide(state, safety_days=2.0, eoq_scale=1.0):
    # Near the end of the run, do not stock more than the days left will sell.
    days_left = max(0, state.days_left)
    low, high = {}, {}
    for product in state.products:
        rate = state.demand_rate[product]
        s = math.ceil(safety_days * rate)
        big_s = min(state.max_storage[product], s + math.ceil(eoq_scale * run_size(state, product)))
        low[product] = min(s, math.ceil(days_left * rate))
        high[product] = min(big_s, math.ceil(days_left * rate))

    # Keep running the current product until it is close to S. "Close" means
    # within one day of sales plus two days' worth of normal ups and downs.
    current = state.setup
    rate = state.demand_rate[current]
    close_to_full = high[current] - math.ceil(rate) - math.ceil(2 * math.sqrt(rate))
    if state.stock[current] < close_to_full and state.stock[current] < high[current]:
        return current

    # Otherwise run the product under s with the fewest days of stock.
    needy = [p for p in state.products if state.stock[p] < low[p]]
    if not needy:
        return "idle"
    neediest = needy[0]
    for product in needy:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product
    return neediest
