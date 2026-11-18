"""Random daily demand around each product's usual rate (the default).

Each day, each product sells a random number of units. The average is the
product's `demand` in the capsule. This is the classic Poisson model, and the
same dice as the research repo.
"""

NAME = "poisson"


def demand(period, rng, model):
    # One random draw per product, all in one call, in product order.
    rates = [model.demand_rate[p] for p in model.products]
    units = rng.poisson(rates)
    return {p: int(units[i]) for i, p in enumerate(model.products)}
