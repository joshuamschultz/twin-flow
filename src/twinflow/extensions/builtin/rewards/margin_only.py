"""Score each day by sales less the cost of making, nothing else.

Switch, holding and lost-sale costs are left out. Compare it with the dollar
score to see how much those costs change the ranking.
"""

NAME = "margin_only"


def reward(move):
    # Sales less the cost of the units made.
    return move.ledger.revenue - move.ledger.production
