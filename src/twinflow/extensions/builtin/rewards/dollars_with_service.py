"""Score each day by its dollars, less a penalty for every unit not delivered.

Use it when an unhappy customer costs more than the lost sale itself. Set the
penalty below, in dollars per unit.
"""

NAME = "dollars_with_service"

# Dollars taken off the score for each unit a customer wanted but did not get.
SETTINGS = {"penalty_per_lost_unit": 2.0}


def reward(move, penalty_per_lost_unit=2.0):
    # Count the units of every product that were asked for but not delivered.
    lost_units = 0.0
    for name, units in move.events.items():
        if name.startswith("lost."):
            lost_units += units
    return move.ledger.total - penalty_per_lost_unit * lost_units
