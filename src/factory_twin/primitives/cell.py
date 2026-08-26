"""COMP-008 Machine + COMP-009 SetupPolicy.

A capacity-N Location is N Machines, each with independent current-setup state.
SetupPolicy decides whether a changeover is owed and how long it takes.
"""

from __future__ import annotations

from factory_twin.primitives.bundle import Bundle


class Machine:
    """One named server carrying its own current-setup state.

    Setup groups, not part locks (D-047): a machine's state is its current
    setup group, never the last part type it ran. Each instance owns its own
    mutable ``current_setup`` — a capacity-N Location is N independent
    Machine instances sharing one SetupPolicy.
    """

    def __init__(self, machine_id: str, initial_setup: str | None = None) -> None:
        self.machine_id = machine_id
        self.current_setup = initial_setup


class SetupPolicy:
    """Resolves a bundle's setup key and looks up changeover time with a flat default.

    Pure decision function (D-047, D-012): given the machine's current setup
    group and an incoming bundle, resolves the bundle's setup group and
    returns the changeover seconds owed plus the setup group to stamp
    afterward. The caller is responsible for stamping the result onto the
    machine.
    """

    def __init__(
        self,
        setup_key_of: dict[str, str],
        changeover_matrix: dict[tuple[str, str], float],
        default_seconds: float,
    ) -> None:
        self.setup_key_of = setup_key_of
        self.changeover_matrix = changeover_matrix
        self.default_seconds = default_seconds

    def changeover(self, current_setup: str | None, bundle: Bundle) -> tuple[float, str]:
        """Return (seconds, new_setup) for moving to bundle's setup group.

        Raises KeyError if bundle.thing has no declared setup key — fail
        closed on an undeclared part type (D-007) rather than default to
        zero changeover time.
        """
        new_setup = self.setup_key_of[bundle.thing]
        if new_setup == current_setup:
            return 0.0, new_setup
        if current_setup is None:
            return self.default_seconds, new_setup
        seconds = self.changeover_matrix.get((current_setup, new_setup), self.default_seconds)
        return seconds, new_setup
