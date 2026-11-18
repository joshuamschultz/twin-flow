"""Make the product that will run out first, but rest when every product is well stocked.

This is `lowest_stock_first` with a higher stop point. It is a candidate: `twinflow improve`
races it against your current rules and copies it into `policies/` only if it wins.
"""

NAME = "keep_stocked"


def decide(state, stop_days=20):
    neediest = min(state.products, key=lambda product: state.days_of_cover[product])
    if state.days_of_cover[neediest] >= stop_days:
        return "idle"
    return neediest
