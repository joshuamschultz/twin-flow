"""Make the product that will run out first.

Count how many days each product's stock lasts at the usual sales rate. Run the
one with the fewest days. If every product has at least `stop_days` days of
stock, rest the press.
"""

NAME = "lowest_cover"

# Values the race can try, to find the best one for this plant.
SETTINGS = {
    "stop_days": [4.0, 6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0, 40.0, 60.0, 1e9],
}


def decide(state, stop_days=1e9):
    # Find the product with the fewest days of stock (on a tie, the first one).
    neediest = state.products[0]
    for product in state.products:
        if state.days_of_cover[product] < state.days_of_cover[neediest]:
            neediest = product

    # Plenty of stock everywhere: rest the press.
    if state.days_of_cover[neediest] >= stop_days:
        return "idle"
    return neediest
