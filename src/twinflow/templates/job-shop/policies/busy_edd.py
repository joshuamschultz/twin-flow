"""Pick the next job at a machine: earliest due date when work piles up, else first in.

This is a dispatch policy. It runs whenever a machine is free and more than one job waits.
`state.queue` lists the waiting jobs. `state.actions` lists the rules you may return.
"""

NAME = "busy_edd"
DECISION = "dispatch"


def decide(state):
    # Two or more jobs waiting: the one due soonest goes first.
    if len(state.queue) > 1:
        return "edd"
    # One job or none: the order does not matter.
    return "fifo"
