"""Never run the press.

A baseline. It shows what the plant earns, or loses, when nothing is made.
"""

NAME = "idle"


def decide(state):
    # Rest the press every day.
    return "idle"
