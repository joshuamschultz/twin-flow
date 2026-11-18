"""Show only the stock of the product on the press.

Every other product looks like it has 8 days of stock (up to its storage limit).
This measures what seeing the whole warehouse is worth.
"""

NAME = "own_product"

# What a product we cannot see looks like, in days of sales.
GUESS_DAYS = 8.0


def observe(state):
    shown = {}
    for p in state.products:
        if p == state.setup:
            shown[p] = state.stock[p]
        else:
            shown[p] = min(state.max_storage[p], round(GUESS_DAYS * state.demand_rate[p]))
    return state.replace(stock=shown)
