"""How full each family is, and which family the press is set up for.

Each family gets the level of its lowest-stocked product: low (under 3 days of
stock), ok (under 10 days) or high. Example: "grey:low black:ok natural:high | grey".
This is the same label as the built-in `family` view.
"""

NAME = "family_cover"

# Levels from low to high: a name and the days of stock it stays under.
LEVELS = [("low", 3), ("ok", 10), ("high", None)]


def level(days):
    # The first level the days of stock fit under.
    for index, band in enumerate(LEVELS):
        below = band[1]  # band is (name, days it stays under)
        if below is None or days < below:
            return index
    return len(LEVELS) - 1


def label(state):
    # The lowest level in each family, families in the order they first appear.
    lowest = {}
    for product in state.products:
        family = state.family[product]
        here = level(state.days_of_cover[product])
        lowest[family] = min(here, lowest.get(family, here))
    stock = " ".join(f"{family}:{LEVELS[index][0]}" for family, index in lowest.items())

    # Then the family on the press ("idle" when nothing is set up).
    on_press = state.family[state.setup] if state.setup is not None else "idle"
    return f"{stock} | {on_press}"
