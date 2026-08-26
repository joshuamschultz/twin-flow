"""COMP-022 WaitClassifier — unit tests (T-034, red phase).

Contract under test (`src/factory_twin/instrumentation/kpis.py::WaitClassifier`,
Layer 4, D-034, structure.md "Fixed order of operations inside a location"):

WaitClassifier reads ONE `ProcessExecution` row's five timestamps
(`queue_arrival_time`, `material_ready_time` [nullable], `actual_start`,
`actual_end`, `release_time`) and splits total wait into three MUTUALLY
EXCLUSIVE categories that SUM TO TOTAL WAIT:

  - STARVED           — no resource (machine/operator) available yet
  - MATERIAL-STARVED   — required input material not ready yet
  - BLOCKED            — the job finished but its output couldn't be released
                          downstream

Total wait on a row = pre-start wait + post-end wait, where:

    pre_start_wait = actual_start - queue_arrival_time
    post_end_wait  = release_time - actual_end            (== BLOCKED, always)
    total_wait     = pre_start_wait + post_end_wait

BLOCKED is independent of the pre-start binding decision and is ALWAYS exactly
`release_time - actual_end` (acceptance criterion 3 — asserted with `==`, not
`approx`).

Pre-start wait (`STARVED` vs `MATERIAL-STARVED`) is entirely attributed, via
the BINDING-CONSTRAINT rule (D-034), to whichever required input became
available LAST. Per structure.md D-045, the fixed order inside a location is
"acquire machine, then operator" BEFORE "pull and consume input material" —
so `actual_start` is the moment the LAST of {resource, material} became
satisfied and the process actually began. `material_ready_time` independently
records when material became available. The rule this test file commits the
implementer to:

  - `material_ready_time is None`
        -> no material gate applies to this row at all; the entire pre-start
           wait is attributed to STARVED (the only tracked constraint).
  - `material_ready_time == actual_start`
        -> material arrived exactly when the process started, i.e. material
           was the LAST-satisfied input (the resource was already free
           earlier and idle waiting) -> the entire pre-start wait is
           MATERIAL-STARVED.
  - `material_ready_time < actual_start`
        -> material was ready before the process started, so the RESOURCE
           was the last-satisfied input -> the entire pre-start wait is
           STARVED.

When pre-start wait is zero (`actual_start == queue_arrival_time`), both
STARVED and MATERIAL-STARVED are exactly 0.0 regardless of `material_ready_time`.

Committed API (this test file fixes it; the implementer conforms):

    WaitClassifier().classify(
        queue_arrival_time: float,
        material_ready_time: float | None,
        actual_start: float,
        actual_end: float,
        release_time: float,
    ) -> tuple[float, float, float]

    Return tuple order: (starved_seconds, blocked_seconds, material_starved_seconds).

`WaitClassifier()` takes no constructor arguments (matches the existing stub's
`__init__(self) -> None`). `classify()` is called once per `ProcessExecution`
row; it does not read Polars, a DataFrame, or any other row shape — five plain
floats (one nullable) in, a 3-tuple of floats out.
"""

from __future__ import annotations

import pytest

from factory_twin.instrumentation.kpis import WaitClassifier

# Fixture rows for the sum-invariant / mutual-exclusivity sweep (acceptance
# criterion 1). Each row is (queue_arrival_time, material_ready_time,
# actual_start, actual_end, release_time, expected_binding_label) where
# expected_binding_label is one of "starved", "material_starved", or "none"
# (no pre-start wait at all).
SUM_INVARIANT_ROWS: list[tuple[float, float | None, float, float, float, str]] = [
    # No wait anywhere: arrives, starts, ends, releases instantly.
    (0.0, 0.0, 0.0, 5.0, 5.0, "none"),
    # Pure resource-starved: material ready long before the resource freed up.
    (0.0, 1.0, 10.0, 15.0, 15.0, "starved"),
    # Pure material-starved: material arrives exactly when work starts.
    (0.0, 8.0, 8.0, 12.0, 12.0, "material_starved"),
    # No material gate at all (nullable): must be treated as resource-starved.
    (0.0, None, 6.0, 9.0, 9.0, "starved"),
    # Resource-starved pre-start wait PLUS an independent blocked tail.
    (0.0, 2.0, 5.0, 10.0, 13.0, "starved"),
    # Material-starved pre-start wait PLUS an independent blocked tail.
    (2.0, 9.0, 9.0, 11.0, 20.0, "material_starved"),
    # Fractional-second timestamps — sum invariant must hold with floats too.
    (0.0, 1.25, 3.75, 4.5, 6.125, "starved"),
]


def _row_total_wait(
    queue_arrival_time: float,
    actual_start: float,
    actual_end: float,
    release_time: float,
) -> float:
    """total wait = pre-start wait + post-end (blocked) wait, per the module docstring."""
    return (actual_start - queue_arrival_time) + (release_time - actual_end)


@pytest.mark.parametrize(
    "queue_arrival_time, material_ready_time, actual_start, actual_end, release_time, binding",
    SUM_INVARIANT_ROWS,
    ids=[
        "no-wait",
        "starved-only",
        "material-starved-only",
        "no-material-gate-tracked",
        "starved-plus-blocked",
        "material-starved-plus-blocked",
        "fractional-seconds",
    ],
)
def test_classify_components_sum_to_total_wait_and_are_mutually_exclusive(
    queue_arrival_time: float,
    material_ready_time: float | None,
    actual_start: float,
    actual_end: float,
    release_time: float,
    binding: str,
) -> None:
    """Acceptance criterion 1: starved + blocked + material_starved == total wait on
    every row, and only the binding pre-start category is nonzero."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=queue_arrival_time,
        material_ready_time=material_ready_time,
        actual_start=actual_start,
        actual_end=actual_end,
        release_time=release_time,
    )

    expected_total = _row_total_wait(queue_arrival_time, actual_start, actual_end, release_time)
    assert starved + blocked + material_starved == pytest.approx(expected_total)

    if binding == "none":
        assert starved == pytest.approx(0.0)
        assert material_starved == pytest.approx(0.0)
    elif binding == "starved":
        assert starved == pytest.approx(actual_start - queue_arrival_time)
        assert material_starved == pytest.approx(0.0)
    elif binding == "material_starved":
        assert material_starved == pytest.approx(actual_start - queue_arrival_time)
        assert starved == pytest.approx(0.0)


def test_classify_resource_available_after_material_attributes_entire_wait_to_starved() -> None:
    """Acceptance criterion 2 (two-cause case): material_ready_time=5,
    actual_start=8 -> the resource was the LAST-satisfied input (freed up at 8,
    three seconds after material was already ready). The whole 8-second
    pre-start wait is STARVED, none of it MATERIAL-STARVED."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=0.0,
        material_ready_time=5.0,
        actual_start=8.0,
        actual_end=8.0,
        release_time=8.0,
    )

    assert starved == pytest.approx(8.0)
    assert material_starved == pytest.approx(0.0)
    assert blocked == pytest.approx(0.0)


def test_classify_material_ready_exactly_at_start_attributes_entire_wait_to_material_starved() -> (
    None
):
    """Acceptance criterion 2 (two-cause case): material_ready_time=8,
    actual_start=8, with the resource free earlier -> material was the
    LAST-satisfied input. The whole 8-second pre-start wait is
    MATERIAL-STARVED, none of it STARVED."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=0.0,
        material_ready_time=8.0,
        actual_start=8.0,
        actual_end=8.0,
        release_time=8.0,
    )

    assert material_starved == pytest.approx(8.0)
    assert starved == pytest.approx(0.0)
    assert blocked == pytest.approx(0.0)


def test_classify_null_material_ready_time_attributes_entire_wait_to_starved() -> None:
    """material_ready_time is nullable (D-034). When it is None, no material gate
    is tracked on this row at all, so the entire pre-start wait is STARVED."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=0.0,
        material_ready_time=None,
        actual_start=6.0,
        actual_end=8.0,
        release_time=8.0,
    )

    assert starved == pytest.approx(6.0)
    assert material_starved == pytest.approx(0.0)
    assert blocked == pytest.approx(0.0)


def test_classify_blocked_equals_release_time_minus_actual_end_exactly() -> None:
    """Acceptance criterion 3: BLOCKED = release_time - actual_end, asserted
    exactly (not approx) — the job finished at actual_end=5 but its output
    wasn't released until release_time=12, a 7-second blocked tail, with zero
    pre-start wait so the row isolates BLOCKED from the other two categories."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=0.0,
        material_ready_time=0.0,
        actual_start=0.0,
        actual_end=5.0,
        release_time=12.0,
    )

    assert blocked == 7.0
    assert starved == pytest.approx(0.0)
    assert material_starved == pytest.approx(0.0)


def test_classify_blocked_and_starved_are_independent_and_both_nonzero() -> None:
    """BLOCKED is computed independently of the pre-start binding decision: a row
    can be STARVED before it starts AND BLOCKED after it ends at the same time,
    and the two categories must not interfere with each other's value."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=0.0,
        material_ready_time=2.0,
        actual_start=5.0,
        actual_end=10.0,
        release_time=13.0,
    )

    assert starved == pytest.approx(5.0)
    assert material_starved == pytest.approx(0.0)
    assert blocked == pytest.approx(3.0)
    assert starved + blocked + material_starved == pytest.approx(8.0)


def test_classify_zero_wait_row_all_three_components_are_exactly_zero() -> None:
    """A row with no pre-start wait and no post-end wait (queue_arrival_time ==
    actual_start, actual_end == release_time) must report all three components
    as exactly zero, not merely summing to zero."""
    classifier = WaitClassifier()

    starved, blocked, material_starved = classifier.classify(
        queue_arrival_time=3.0,
        material_ready_time=3.0,
        actual_start=3.0,
        actual_end=9.0,
        release_time=9.0,
    )

    assert starved == 0.0
    assert blocked == 0.0
    assert material_starved == 0.0
