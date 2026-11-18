"""Make the product that will run out first.

Look at every product and count how many days its stock will last at the
usual sales rate. Run the one with the fewest days left. If every product
has more than `stop_days` days of stock, rest the press.
"""

NAME = "lowest_stock_first"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"stop_days": [6, 10, 15, 25]}


def decide(state, stop_days=1000):
    # Start with the first product, then look for one that runs out sooner.
    neediest = state.products[0]
    for product in state.products:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product

    # Plenty of stock everywhere: rest the press and save the setup cost.
    if state.days_of_cover[neediest] >= stop_days:
        return "idle"
    return neediest
