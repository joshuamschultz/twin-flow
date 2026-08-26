"""COMP-013 PullRule — unit tests (T-016, red phase).

PullRule (primitives/location.py) decides WHAT a Location takes from its
queue and HOW MUCH, ahead of the fixed order of operations' step 1: "Pull
from the queue per the location's pull rule (D-051), or wait for the first
arrival" (structure.md).

  * DEFAULT (no rule declared): take the queue in ARRIVAL ORDER. This is the
    engine default per D-037/D-040 ("A dispatch-rule engine chosen on the
    client's behalf moves their promised dates on the strength of your
    pick" — arrival order, always). The fact that the default applied is
    recorded so the report's Assumptions block can name it (D-027).
  * BATCH THRESHOLD, expressed in the thing's OWN unit of measure (e.g.
    "200 pieces" or "500 lb"): accumulate eligible bundles, in arrival
    order, until the summed qty in that uom reaches the threshold, then
    stop. The selected set must sum to the threshold exactly when the
    fixture is crafted to land on it — not more, not less.
  * ELIGIBILITY: only bundles whose thing resolves (via the same
    setup_key_of mapping SetupPolicy uses, D-047/D-012) to the machine's
    CURRENT setup group are eligible. An ineligible bundle is skipped
    entirely — it is neither selected nor counted toward a batch total —
    and arrival order among the remaining eligible bundles is preserved.

Public API committed here (implementer conforms):

    rule = PullRule(
        setup_key_of: dict[str, str],   # part_type -> setup_group, same
                                         # mapping shape as SetupPolicy
        spec: str | None = None,        # None => arrival-order default;
                                         # "<value> <uom>" => batch threshold
                                         # in that uom, e.g. "200 pieces"
    )
        rule.default_applied -> bool, True iff spec is None. Observable
                                 record for the Assumptions block.

    selected: list[Bundle] = rule.select(
        queue: Sequence[Bundle], current_setup: str | None
    )
        # empty list when nothing in queue is eligible for current_setup

Pure Layer 1 primitives: no YAML, no file I/O, no SimPy. Bundles are built
here from plain Python values only.
"""

from __future__ import annotations

from twinflow.primitives.bundle import Bundle
from twinflow.primitives.location import PullRule

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# Two part types share setup group "line_a"; one lives in "line_b"; one in
# "line_c" and is used purely as an ineligible interloper in the queue.
SETUP_KEY_OF = {
    "widget_a": "line_a",
    "widget_b": "line_a",
    "casting": "line_b",
    "off_type": "line_c",
}


def _bundle(thing: str, qty: float, uom: str = "pieces") -> Bundle:
    return Bundle(qty=qty, thing=thing, uom=uom)


def _make_default_rule() -> PullRule:
    return PullRule(setup_key_of=SETUP_KEY_OF, spec=None)


def _make_threshold_rule(spec: str) -> PullRule:
    return PullRule(setup_key_of=SETUP_KEY_OF, spec=spec)


# ---------------------------------------------------------------------------
# default_applied — the observable record for the Assumptions block
# ---------------------------------------------------------------------------


def test_default_applied_is_true_when_no_spec_declared() -> None:
    rule = _make_default_rule()

    assert rule.default_applied is True


def test_default_applied_is_false_when_batch_threshold_declared() -> None:
    rule = _make_threshold_rule("200 pieces")

    assert rule.default_applied is False


# ---------------------------------------------------------------------------
# DEFAULT — arrival order, no rule declared
# ---------------------------------------------------------------------------


def test_select_with_no_spec_returns_queue_in_arrival_order() -> None:
    rule = _make_default_rule()
    queue = [
        _bundle("widget_a", 10),
        _bundle("widget_b", 20),
        _bundle("widget_a", 30),
    ]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == queue


def test_select_with_no_spec_skips_bundles_outside_current_setup() -> None:
    rule = _make_default_rule()
    queue = [
        _bundle("widget_a", 10),
        _bundle("off_type", 5),
        _bundle("widget_b", 20),
    ]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == [
        _bundle("widget_a", 10),
        _bundle("widget_b", 20),
    ]


def test_select_with_no_spec_and_empty_queue_returns_empty_list() -> None:
    rule = _make_default_rule()

    selected = rule.select([], current_setup="line_a")

    assert selected == []


def test_select_returns_empty_list_when_nothing_eligible_for_current_setup() -> None:
    rule = _make_default_rule()
    queue = [_bundle("off_type", 10), _bundle("casting", 20, uom="lb")]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == []


def test_select_with_current_setup_none_selects_nothing() -> None:
    rule = _make_default_rule()
    queue = [_bundle("widget_a", 10), _bundle("casting", 20, uom="lb")]

    selected = rule.select(queue, current_setup=None)

    assert selected == []


def test_select_does_not_mutate_input_queue() -> None:
    rule = _make_default_rule()
    queue = [_bundle("widget_a", 10), _bundle("off_type", 5), _bundle("widget_b", 20)]
    original = list(queue)

    rule.select(queue, current_setup="line_a")

    assert queue == original


# ---------------------------------------------------------------------------
# BATCH THRESHOLD — own unit of measure, both a piece-count and a weight uom
# ---------------------------------------------------------------------------


def test_select_batch_threshold_pieces_stops_at_threshold() -> None:
    rule = _make_threshold_rule("200 pieces")
    queue = [
        _bundle("widget_a", 50, uom="pieces"),
        _bundle("widget_a", 80, uom="pieces"),
        _bundle("widget_a", 70, uom="pieces"),  # cumulative 200 here
        _bundle("widget_a", 40, uom="pieces"),  # must NOT be included
    ]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == queue[:3]
    assert sum(b.qty for b in selected) == 200


def test_select_batch_threshold_lb_stops_at_threshold() -> None:
    rule = _make_threshold_rule("500 lb")
    queue = [
        _bundle("casting", 200, uom="lb"),
        _bundle("casting", 150, uom="lb"),
        _bundle("casting", 150, uom="lb"),  # cumulative 500 here
        _bundle("casting", 50, uom="lb"),  # must NOT be included
    ]

    selected = rule.select(queue, current_setup="line_b")

    assert selected == queue[:3]
    assert sum(b.qty for b in selected) == 500


def test_select_batch_threshold_skips_bundles_outside_current_setup() -> None:
    rule = _make_threshold_rule("200 pieces")
    queue = [
        _bundle("widget_a", 100, uom="pieces"),
        _bundle("off_type", 500, uom="pieces"),  # ineligible: not counted, not selected
        _bundle("widget_a", 100, uom="pieces"),  # cumulative 200 here
    ]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == [queue[0], queue[2]]
    assert sum(b.qty for b in selected) == 200


def test_select_batch_threshold_returns_all_eligible_when_total_below_threshold() -> None:
    rule = _make_threshold_rule("1000 pieces")
    queue = [
        _bundle("widget_a", 50, uom="pieces"),
        _bundle("widget_a", 80, uom="pieces"),
    ]

    selected = rule.select(queue, current_setup="line_a")

    assert selected == queue
    assert sum(b.qty for b in selected) == 130
