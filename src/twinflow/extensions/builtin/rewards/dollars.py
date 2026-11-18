"""Score each day by its dollars: sales less every cost.

This is the default score. It is the same number as the dollar ledger.
"""

NAME = "dollars"


def reward(move):
    # Sales less making, switch, holding, lost-sale and late costs.
    return move.ledger.total
