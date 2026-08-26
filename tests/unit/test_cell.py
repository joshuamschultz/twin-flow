"""COMP-008 Machine and COMP-009 SetupPolicy — unit tests (T-012, red phase).

Machine (primitives/cell.py): one named server carrying its OWN current-setup
state. A capacity-N Location is N Machines, each with independent setup state
(structure.md "Setup groups, not part locks", D-047). `current_setup` is the
value the changeover decision reads and stamps — never "the last part type".

SetupPolicy (primitives/cell.py): decides whether a changeover is owed and how
long it takes.

  * Resolves an incoming Bundle's SETUP KEY via a declared part-type ->
    setup-group mapping (D-012, D-047). Two part types sharing a setup group
    interleave at zero changeover cost.
  * A part type outside the machine's current setup group pays the declared
    CHANGEOVER MATRIX time, keyed by (from_group, to_group); pairs absent from
    the matrix fall back to a flat default (D-027's "setup 0" default is a
    modeling floor, not this policy's default — this default is the declared
    flat fallback for an undeclared pair).

Public API committed here (implementer conforms at T-013):

    m = Machine(machine_id: str, initial_setup: str | None = None)
        m.machine_id      -> str, stored verbatim
        m.current_setup   -> str | None, mutable — the machine's own state

    policy = SetupPolicy(
        setup_key_of: dict[str, str],           # part_type -> setup_group
        changeover_matrix: dict[tuple[str, str], float],  # (from, to) -> seconds
        default_seconds: float,                 # flat fallback for undeclared pairs
    )

    seconds, new_setup = policy.changeover(current_setup: str | None, bundle: Bundle)
        # seconds    -> 0.0 when bundle's setup group == current_setup
        # new_setup  -> the setup group to stamp onto the machine afterward
        # Caller is responsible for stamping: machine.current_setup = new_setup

Pure Layer 1 primitives: no YAML, no file I/O, no SimPy. Setup math is
synchronous. Bundles are built here from plain Python values only.
"""

from __future__ import annotations

import pytest

from factory_twin.primitives.bundle import Bundle
from factory_twin.primitives.cell import Machine, SetupPolicy

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Two part types sharing one setup group ("grinder_coarse"); a third part
# type lives in a different group ("grinder_fine"); a fourth is declared but
# has no matrix entry against either of the other two groups, so it exercises
# the flat-default fallback.
SETUP_KEY_OF = {
    "blank_a": "grinder_coarse",
    "blank_b": "grinder_coarse",
    "blank_fine": "grinder_fine",
    "blank_odd": "grinder_odd",
}

CHANGEOVER_MATRIX = {
    ("grinder_coarse", "grinder_fine"): 45.0,
    ("grinder_fine", "grinder_coarse"): 30.0,
}

DEFAULT_SECONDS = 120.0


def _make_policy() -> SetupPolicy:
    return SetupPolicy(
        setup_key_of=SETUP_KEY_OF,
        changeover_matrix=CHANGEOVER_MATRIX,
        default_seconds=DEFAULT_SECONDS,
    )


def _bundle(part_type: str, qty: float = 1.0) -> Bundle:
    return Bundle(qty=qty, thing=part_type, uom="piece")


# ---------------------------------------------------------------------------
# Machine — COMP-008
# ---------------------------------------------------------------------------


def test_machine_exposes_machine_id_verbatim() -> None:
    machine = Machine("grinder_1", initial_setup=None)

    assert machine.machine_id == "grinder_1"


def test_machine_current_setup_defaults_to_initial_setup() -> None:
    machine = Machine("grinder_1", initial_setup="grinder_coarse")

    assert machine.current_setup == "grinder_coarse"


def test_machine_with_no_initial_setup_starts_with_none() -> None:
    machine = Machine("grinder_1", initial_setup=None)

    assert machine.current_setup is None


def test_machine_current_setup_is_independently_mutable_per_instance() -> None:
    machine_a = Machine("grinder_1", initial_setup="grinder_coarse")
    machine_b = Machine("grinder_2", initial_setup="grinder_coarse")

    machine_a.current_setup = "grinder_fine"

    assert machine_a.current_setup == "grinder_fine"
    assert machine_b.current_setup == "grinder_coarse"


# ---------------------------------------------------------------------------
# SetupPolicy — COMP-009
# Acceptance 1: same setup group interleaves at zero changeover cost
# ---------------------------------------------------------------------------


def test_changeover_within_same_setup_group_costs_zero_seconds() -> None:
    policy = _make_policy()

    # blank_a and blank_b both resolve to "grinder_coarse".
    seconds, _new_setup = policy.changeover("grinder_coarse", _bundle("blank_b"))

    assert seconds == 0.0


def test_changeover_within_same_setup_group_leaves_setup_group_unchanged() -> None:
    policy = _make_policy()

    _seconds, new_setup = policy.changeover("grinder_coarse", _bundle("blank_a"))

    assert new_setup == "grinder_coarse"


def test_changeover_interleaving_two_part_types_in_same_group_stays_zero_each_time() -> None:
    """Two part types sharing a setup key interleave at zero cost, repeatedly."""
    policy = _make_policy()
    current_setup = "grinder_coarse"

    for part_type in ("blank_a", "blank_b", "blank_a", "blank_b"):
        seconds, current_setup = policy.changeover(current_setup, _bundle(part_type))
        assert seconds == 0.0
        assert current_setup == "grinder_coarse"


# ---------------------------------------------------------------------------
# SetupPolicy — COMP-009
# Acceptance 2: part type outside current setup pays the declared matrix time,
# falling back to a flat default when the pair is undeclared
# ---------------------------------------------------------------------------


def test_changeover_to_declared_pair_charges_exact_matrix_seconds() -> None:
    policy = _make_policy()

    seconds, new_setup = policy.changeover("grinder_coarse", _bundle("blank_fine"))

    assert seconds == 45.0
    assert new_setup == "grinder_fine"


def test_changeover_matrix_is_directional_not_symmetric() -> None:
    policy = _make_policy()

    seconds, new_setup = policy.changeover("grinder_fine", _bundle("blank_a"))

    assert seconds == 30.0
    assert new_setup == "grinder_coarse"


def test_changeover_to_undeclared_pair_falls_back_to_flat_default() -> None:
    policy = _make_policy()

    # (grinder_coarse, grinder_odd) has no matrix entry.
    seconds, new_setup = policy.changeover("grinder_coarse", _bundle("blank_odd"))

    assert seconds == DEFAULT_SECONDS
    assert new_setup == "grinder_odd"


def test_changeover_from_a_cold_machine_with_no_initial_setup_pays_default() -> None:
    """A machine with current_setup=None has no declared matrix entry against
    any group, so the first job it runs pays the flat default — never zero."""
    policy = _make_policy()

    seconds, new_setup = policy.changeover(None, _bundle("blank_a"))

    assert seconds == DEFAULT_SECONDS
    assert new_setup == "grinder_coarse"


def test_changeover_on_undeclared_part_type_raises_key_error() -> None:
    """A bundle whose part type has no declared setup key is a modeling error,
    not a silent zero — the policy must fail closed (D-007)."""
    policy = _make_policy()

    with pytest.raises(KeyError):
        policy.changeover("grinder_coarse", _bundle("nonexistent_part"))


# ---------------------------------------------------------------------------
# Acceptance 3: capacity-2 Location's two Machines hold independent setup state
# ---------------------------------------------------------------------------


def test_two_machines_at_one_location_hold_independent_setup_after_changeover() -> None:
    """Simulates a capacity-2 location: two Machine instances, one SetupPolicy.
    Running a changeover on machine A and stamping its result must not affect
    machine B's own current_setup."""
    policy = _make_policy()
    machine_a = Machine("grinder_1", initial_setup="grinder_coarse")
    machine_b = Machine("grinder_2", initial_setup="grinder_coarse")

    seconds, new_setup = policy.changeover(machine_a.current_setup, _bundle("blank_fine"))
    machine_a.current_setup = new_setup

    assert seconds == 45.0
    assert machine_a.current_setup == "grinder_fine"
    assert machine_b.current_setup == "grinder_coarse"


def test_two_machines_process_different_groups_simultaneously_without_interference() -> None:
    """Machine A serves grinder_fine work while machine B keeps serving
    grinder_coarse work; neither machine's state leaks into the other."""
    policy = _make_policy()
    machine_a = Machine("grinder_1", initial_setup="grinder_fine")
    machine_b = Machine("grinder_2", initial_setup="grinder_coarse")

    seconds_a, new_setup_a = policy.changeover(machine_a.current_setup, _bundle("blank_fine"))
    machine_a.current_setup = new_setup_a

    seconds_b, new_setup_b = policy.changeover(machine_b.current_setup, _bundle("blank_b"))
    machine_b.current_setup = new_setup_b

    assert seconds_a == 0.0
    assert machine_a.current_setup == "grinder_fine"
    assert seconds_b == 0.0
    assert machine_b.current_setup == "grinder_coarse"
