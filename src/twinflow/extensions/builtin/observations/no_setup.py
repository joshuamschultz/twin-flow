"""Hide which product the press is set up for.

The policy is told the first product is on the press, whatever is really on it.
This measures what knowing the setup is worth.
"""

NAME = "no_setup"


def observe(state):
    # Pretend the first product is on the press.
    return state.replace(setup=state.products[0])
