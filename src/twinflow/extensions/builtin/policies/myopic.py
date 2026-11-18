"""Pick the most money today. Skip tomorrow.

For each choice (rest the press, or run one product) work out today's expected
money: sales at the usual demand, less making cost, switch cost, holding cost
and lost sales. With `salvage`, stock left at the end of the day counts at what
it cost to make, so building stock is not a pure loss.
"""

import math

import numpy

NAME = "myopic"

# Expected units sold, remembered so each one is worked out only once.
remembered = {}


def expected_sold(rate, on_hand):
    # Average units sold when demand is random around `rate` and `on_hand` are in stock.
    key = (rate, on_hand)
    if key not in remembered:
        counts = numpy.arange(0, int(rate + 10 * math.sqrt(rate) + 30))
        log_odds = (
            counts * math.log(rate) - rate - numpy.array([math.lgamma(k + 1) for k in counts])
        )
        remembered[key] = float(numpy.minimum(on_hand, counts) @ numpy.exp(log_odds))
    return remembered[key]


def money_today(state, stock, choice, salvage):
    # Units made today: a switch loses part of the day; never overfill storage.
    made = {p: 0 for p in state.products}
    switch_cost = 0.0
    if choice != "idle":
        if choice == state.setup:
            lost_share = 0.0
        else:
            same = state.family[choice] == state.family[state.setup]
            switch = state.changeover["within_family" if same else "across_family"]
            lost_share, switch_cost = switch["output_lost"], switch["cost"]
        room = state.max_storage[choice] - stock[choice]
        made[choice] = max(0, min(round(state.run_rate[choice] * (1.0 - lost_share)), room))

    on_hand = {p: stock[p] + made[p] for p in state.products}
    sold = {
        p: expected_sold(state.demand_rate[p], min(on_hand[p], state.max_storage[p]))
        for p in state.products
    }
    left = {p: on_hand[p] - sold[p] for p in state.products}

    revenue = sum(sold[p] * state.price[p] for p in state.products)
    making = sum(made[p] * state.unit_cost[p] for p in state.products)
    holding = sum(state.holding_cost[p] * left[p] for p in state.products)
    lost = sum(state.lost_sale_cost[p] * (state.demand_rate[p] - sold[p]) for p in state.products)
    kept = sum(left[p] * state.unit_cost[p] for p in state.products) if salvage else 0.0
    return revenue - making - switch_cost - holding - lost + kept


def decide(state, salvage=True):
    # Whole units in stock.
    stock = {p: round(state.stock[p]) for p in state.products}

    # Try resting first, then each product; keep the first best one.
    best, best_money = None, None
    for choice in ["idle", *state.products]:
        money = money_today(state, stock, choice, salvage)
        if best_money is None or money > best_money:
            best, best_money = choice, money
    return best
