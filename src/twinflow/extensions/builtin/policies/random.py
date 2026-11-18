"""Pick any product, or rest the press, at random.

A baseline. A good rule should beat it by a lot. The dice start the same way
every time the file is loaded, so a race can be repeated.
"""

import numpy

NAME = "random"

# The dice. Seed 0, the same as the research repo.
dice = numpy.random.default_rng(0)


def decide(state):
    # Every choice in state.actions is equally likely.
    pick = int(dice.integers(len(state.actions)))
    return state.actions[pick]
