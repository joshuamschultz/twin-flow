"""How full each ABC class is, and which family the press is set up for.

Each class (A fast sellers, B, C slow sellers) gets the level of its
lowest-stocked product: low (under 3 days of stock), ok (under 10 days) or high.
Example: "A:low B:ok C:high | grey".
"""

NAME = "abc_cover"

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
    # The lowest level in each class, in the order A, B, C.
    lowest = {}
    for product in state.products:
        here = level(state.days_of_cover[product])
        cls = state.abc[product]
        lowest[cls] = min(here, lowest.get(cls, here))
    stock = " ".join(f"{cls}:{LEVELS[lowest[cls]][0]}" for cls in sorted(lowest))

    # Then the family on the press ("idle" when nothing is set up).
    on_press = state.family[state.setup] if state.setup is not None else "idle"
    return f"{stock} | {on_press}"
