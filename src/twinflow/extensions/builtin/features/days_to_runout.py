"""How soon the plant runs out of something.

Adds two values: "days_to_runout", the fewest days of stock of any product,
and "days_to_runout_on_line", the fewest days in the family the press is set
up for.
"""

NAME = "days_to_runout"


def features(state):
    # The product that runs out first, anywhere.
    soonest = min(state.days_of_cover[p] for p in state.products)

    # The product that runs out first in the family on the press.
    on_line = soonest
    if state.setup is not None:
        family = state.family[state.setup]
        on_line = min(state.days_of_cover[p] for p in state.products if state.family[p] == family)
    return {"days_to_runout": soonest, "days_to_runout_on_line": on_line}
