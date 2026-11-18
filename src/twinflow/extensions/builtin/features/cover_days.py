"""Days of stock for every product, as numbers a policy or a model can use.

Adds one value per product, named "cover_days.<product>". It is the same as
`state.days_of_cover`, recorded with every state so it can be studied later.
"""

NAME = "cover_days"


def features(state):
    # Days each product's stock lasts at the usual sales rate.
    return {f"cover_days.{p}": state.days_of_cover[p] for p in state.products}
