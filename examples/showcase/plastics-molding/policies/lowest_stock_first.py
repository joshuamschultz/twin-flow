"""Make the bin that will run out first.

This is a policy file: it picks what press 3 makes each day. It is the planner's rule
today. Look at every bin and count how many days its stock will last at the usual sales
rate. Run the one with the fewest days left. If every bin has more than `stop_days` days
of stock, rest the press.
"""

NAME = "lowest_stock_first"

# Values the race may try. <- CHANGE: when every bin has this many days of stock, rest.
SETTINGS = {"stop_days": [6, 10, 15, 25]}


def decide(state, stop_days=1000):
    # Start with the first bin, then look for one that runs out sooner.
    neediest = state.products[0]
    for product in state.products:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product

    # Plenty of stock everywhere: rest the press and save the setup cost.
    if state.days_of_cover[neediest] >= stop_days:
        return "idle"
    # KEY LINE: run the bin that runs out first, even if it is a new color.
    return neediest
