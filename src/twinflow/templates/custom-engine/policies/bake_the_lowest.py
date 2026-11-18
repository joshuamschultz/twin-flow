"""Bake the bread that will run out first. Rest when every bread has 4 days of stock."""

NAME = "bake_the_lowest"
SETTINGS = {"stop_days": [2, 4, 8]}


def decide(state, stop_days=4):
    neediest = min(state.products, key=lambda product: state.days_of_cover[product])
    if state.days_of_cover[neediest] >= stop_days:
        return "idle"
    return neediest
