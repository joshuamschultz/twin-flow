"""Run every product in the same order, a set number of days each.

The order keeps each family together, so most switches are cheap ones inside a
family. This is the classic "product wheel".
"""

NAME = "fixed_wheel"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"days_per_slot": [1, 2, 3, 4, 5, 6]}


def wheel_order(state):
    # Families in the order they first appear, then the products of each family.
    families = []
    for product in state.products:
        if state.family[product] not in families:
            families.append(state.family[product])
    order = []
    for family in families:
        for product in state.products:
            if state.family[product] == family:
                order.append(product)
    return order


def decide(state, days_per_slot=1):
    # Each slot of the wheel lasts days_per_slot days.
    order = wheel_order(state)
    slot = state.period // days_per_slot
    return order[slot % len(order)]
