"""Run products in a set order. Fast sellers get a turn often, slow sellers less often.

The wheel turns four times. A products run on every turn, B products on every
second turn, C products on every fourth turn.
"""

NAME = "abc_wheel"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"days_per_slot": [1, 2, 3, 4, 5, 6]}

# How often each class comes round: every turn, every 2nd turn, every 4th turn.
EVERY = {"A": 1, "B": 2, "C": 4}


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
    # Write out the four turns, skipping products that do not run on a turn.
    sequence = []
    for turn in range(4):
        for product in wheel_order(state):
            if turn % EVERY[state.abc[product]] == 0:
                sequence.append(product)

    # Each slot lasts days_per_slot days.
    slot = state.period // days_per_slot
    return sequence[slot % len(sequence)]
