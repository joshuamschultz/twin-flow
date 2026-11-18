"""Make what runs out first, but count a product switch as lost days.

Each product's days of stock gets a penalty when making it needs a switch:
`within_penalty_days` for a product in the same family, `cross_penalty_days`
for a new family. Run the product with the lowest total. If every product has
at least `stop_days` days of stock, rest the press.
"""

NAME = "setup_aware_runout"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"cross_penalty_days": [0.5, 1.5, 3.0, 6.0], "within_penalty_days": [0.0, 1.0, 2.0]}


def decide(state, cross_penalty_days=6.0, within_penalty_days=1.0, stop_days=15.0):
    # Plenty of stock everywhere: rest the press.
    if min(state.days_of_cover.values()) >= stop_days:
        return "idle"

    best, best_score = None, None
    for product in state.products:
        # The penalty for the switch this product needs.
        if product == state.setup:
            penalty = 0.0
        elif state.family[product] == state.family[state.setup]:
            penalty = within_penalty_days
        else:
            penalty = cross_penalty_days
        score = state.days_of_cover[product] + penalty

        # Keep the lowest score (on a tie, the first product).
        if best_score is None or score < best_score:
            best, best_score = product, score
    return best
