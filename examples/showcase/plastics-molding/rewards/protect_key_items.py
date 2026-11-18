"""Score each day by its dollars, less a fine for every key bin we could not ship.

This is a reward file. Our largest customer stocks three bins on its shelves. Its
contract fines us for each unit we cannot ship of those bins. The fine is on top of
the lost sale itself, so the race should rank rules by dollars after fines.
"""

NAME = "protect_key_items"

# <- CHANGE: the bins in the customer's contract, and the fine per unit short, in dollars.
SETTINGS = {"key_items": ["GRY-S", "GRY-M", "BLK-S"], "fine_per_unit": 3.0}


def reward(move, key_items=(), fine_per_unit=0.0):
    # Units asked for but not shipped show up as events named "lost.<bin>".
    units_short = 0.0
    for name, units in move.events.items():
        if name.startswith("lost.") and name[len("lost."):] in key_items:
            units_short += units
    # KEY LINE: the day's dollars (sales less every cost) less the contract fine.
    return move.ledger.total - fine_per_unit * units_short
