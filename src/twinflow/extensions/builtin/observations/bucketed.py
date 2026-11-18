"""Show stock only as low, medium or high.

Each product's stock is replaced by a typical stock for its level: 1.5 days of
sales when low (under 3 days), 6 days when medium (under 10 days), 15 days when
high, never more than the storage limit. This measures what exact counts are worth.
"""

NAME = "bucketed"

# Days of stock that count as low (under 3) or medium (under 10); the rest is high.
LOW_BELOW, MEDIUM_BELOW = 3.0, 10.0

# The stock shown for each level, in days of sales.
SHOWN_DAYS = {"low": 1.5, "medium": 6.0, "high": 15.0}


def observe(state):
    shown = {}
    for p in state.products:
        days = state.days_of_cover[p]
        level = "low" if days < LOW_BELOW else "medium" if days < MEDIUM_BELOW else "high"
        typical = round(SHOWN_DAYS[level] * state.demand_rate[p])
        shown[p] = min(state.max_storage[p], typical)
    return state.replace(stock=shown)
