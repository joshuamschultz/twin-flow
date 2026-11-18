"""Hide the day, so the policy cannot see the end of the run coming.

The day is shown as -1. Rules that wind down stock before the end can no longer
do it. This measures what knowing the end date is worth.
"""

NAME = "no_days_left"


def observe(state):
    # Day -1 means "unknown"; days left is then the whole run and one more day.
    return state.replace(period=-1)
