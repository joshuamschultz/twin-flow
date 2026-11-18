"""Random daily demand with a sales lift on promotion days.

On the days listed in `periods`, the products in `products` ("all" for every
product) sell `lift` times their usual rate. Every other day is normal.
"""

NAME = "promotion"

# Change these to fit your promotion calendar.
SETTINGS = {"periods": [20, 21, 22, 23, 24], "lift": 1.5, "products": "all"}


def demand(period, rng, model, periods=(), lift=1.0, products="all"):
    rates = []
    for p in model.products:
        on_promotion = period in periods and (products == "all" or p in products)
        rates.append(model.demand_rate[p] * (lift if on_promotion else 1.0))
    units = rng.poisson(rates)
    return {p: int(units[i]) for i, p in enumerate(model.products)}
