"""Machine and operator-pool timelines for the baseline heuristic (T-100, T-101).

`MachineTimeline.find_slot` is checked for machine availability, the attached and
detached material gate, setups from the machine state and from the previous occupation,
gap insertion that never changes a follower's setup, calendar `forbid` and `pause`, and
the slot filter used to search jointly with operator pools. `PoolTimeline` is checked for
per-phase capacity and pool calendars, and batch machines for join versus new batch.

Components: COMP-009, COMP-016. Requirements: REQ-075.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pytest

from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import (
    FamilyMatrixEntry,
    Job,
    Machine,
    MachineState,
    Operation,
    Problem,
    ProcessingOption,
    SetupFunction,
    WorkCenter,
)
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.timeline import (
    MachineTimeline,
    PoolNeed,
    PoolTimeline,
    Slot,
    SlotRequest,
    effective_calendar,
    hold_operators,
    operator_retry,
)

M = "M"
CHANGEOVER = 5.0
FAMILY_SETUP = SetupFunction(
    id="family",
    terms=(),
    family_matrix=tuple(
        FamilyMatrixEntry(from_family=a, to_family=b, seconds=0.0 if a == b else CHANGEOVER)
        for a in ("X", "Y")
        for b in ("X", "Y")
    ),
    default_seconds=0.0,
)
INF = float("inf")


# --- builders ------------------------------------------------------------------------------


def _op(op_id: str, family: str | None = None) -> Operation:
    return Operation(
        id=op_id,
        work_center_id="WC",
        quantity=1.0,
        options=(
            ProcessingOption(machine_id=M, fixed_seconds=1, unit_seconds=0, preference_cost=0),
        ),
        setup_family=family,
    )


def _table(
    families: Mapping[str, str | None],
    *,
    function: SetupFunction | None = FAMILY_SETUP,
    state_family: str | None = None,
    machine_id: str = M,
) -> SetupTable:
    """A setup table for one machine over one operation per entry of `families`."""
    problem = Problem(
        id="p",
        jobs=tuple(
            Job(id=f"J-{op_id}", operations=(_op(op_id, family),))
            for op_id, family in families.items()
        ),
        machines=(
            Machine(
                id=machine_id,
                work_center_id="WC",
                setup_function_id=function.id if function else None,
                state=MachineState(last_setup_family=state_family),
            ),
        ),
        work_centers=(WorkCenter(id="WC"),),
        setup_functions=(function,) if function else (),
    )
    return SetupTable.for_problem(problem)


def _request(
    op_id: str,
    run: float,
    *,
    earliest: float = 0.0,
    gate: float = 0.0,
    detached: bool = False,
    pause: bool = False,
    calendar: Intervals = None,
    quantity: float = 1.0,
    family: str | None = None,
) -> SlotRequest:
    return SlotRequest(
        op_id=op_id,
        run_seconds=run,
        earliest_start=earliest,
        gate_ready=gate,
        quantity=quantity,
        detached=detached,
        shift_crossing="pause" if pause else "forbid",
        calendar=calendar,
        batch_family=family,
    )


def _place(timeline: MachineTimeline, request: SlotRequest) -> Slot:
    slot = timeline.find_slot(request)
    assert slot is not None, request
    timeline.place(request, slot)
    return slot


def _times(slot: Slot | None) -> tuple[float, float, float] | None:
    return None if slot is None else (slot.start, slot.setup_end, slot.end)


def _assert_setups_match_start_order(table: SetupTable, placed: dict[str, Slot]) -> None:
    """Every op's setup equals SetupTable.seconds against its predecessor by start order."""
    previous: str | None = None
    previous_end = -INF
    for op_id, slot in sorted(placed.items(), key=lambda item: item[1].start):
        expected = table.seconds(M, previous, op_id)
        assert slot.setup_seconds == expected, (op_id, previous)
        assert slot.setup_end - slot.start == expected, op_id
        assert slot.start >= previous_end, (op_id, previous)
        previous, previous_end = op_id, slot.end


# --- MachineTimeline: availability, gate, setup source ----------------------------------------


def test_find_slot_on_empty_machine_starts_at_the_latest_lower_bound() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=4.0)

    assert _times(timeline.find_slot(_request("a", 5, earliest=2, gate=3))) == (4, 4, 9)
    assert _times(timeline.find_slot(_request("a", 5, earliest=6, gate=3))) == (6, 6, 11)
    assert _times(timeline.find_slot(_request("a", 5, earliest=2, gate=7))) == (7, 7, 12)


def test_find_slot_takes_the_first_setup_from_the_machine_state() -> None:
    table = _table({"a": "Y"}, state_family="X")
    timeline = MachineTimeline(M, table, available_from=0.0)

    slot = timeline.find_slot(_request("a", 5))

    assert _times(slot) == (0, CHANGEOVER, CHANGEOVER + 5)
    assert slot is not None and slot.setup_seconds == CHANGEOVER


def test_attached_setup_waits_for_the_material_gate() -> None:
    timeline = MachineTimeline(M, _table({"a": "Y"}, state_family="X"), available_from=0.0)

    assert _times(timeline.find_slot(_request("a", 5, gate=10))) == (10, 15, 20)


def test_detached_setup_runs_before_the_material_gate_while_the_machine_is_free() -> None:
    timeline = MachineTimeline(M, _table({"a": "Y"}, state_family="X"), available_from=0.0)

    assert _times(timeline.find_slot(_request("a", 5, gate=10, detached=True))) == (5, 10, 15)


def test_detached_setup_still_respects_available_from() -> None:
    timeline = MachineTimeline(M, _table({"a": "Y"}, state_family="X"), available_from=8.0)

    assert _times(timeline.find_slot(_request("a", 5, gate=10, detached=True))) == (8, 13, 18)


def test_find_slot_appends_after_the_last_occupation_with_its_setup() -> None:
    table = _table({"a": "X", "b": "Y"})
    timeline = MachineTimeline(M, table, available_from=0.0)
    _place(timeline, _request("a", 10))

    slot = timeline.find_slot(_request("b", 4))

    assert _times(slot) == (10, 15, 19)


# --- gap insertion -----------------------------------------------------------------------------


def test_gap_insertion_uses_a_gap_when_both_setups_fit() -> None:
    table = _table({"a": "X", "c": "X", "b": "X"}, state_family="X")
    timeline = MachineTimeline(M, table, available_from=0.0)
    placed = {"a": _place(timeline, _request("a", 10))}
    placed["c"] = _place(timeline, _request("c", 10, earliest=30))

    placed["b"] = _place(timeline, _request("b", 5))

    assert _times(placed["b"]) == (10, 10, 15)
    _assert_setups_match_start_order(table, placed)


def test_gap_insertion_rejects_a_gap_that_would_change_the_followers_setup() -> None:
    table = _table({"a": "X", "c": "X", "b": "Y"}, state_family="X")
    timeline = MachineTimeline(M, table, available_from=0.0)
    placed = {"a": _place(timeline, _request("a", 10))}
    placed["c"] = _place(timeline, _request("c", 10, earliest=30))

    placed["b"] = _place(timeline, _request("b", 5))

    assert _times(placed["b"]) == (40, 45, 50)
    _assert_setups_match_start_order(table, placed)


def test_gap_insertion_rejects_a_gap_too_short_for_setup_and_run() -> None:
    table = _table({"a": "X", "c": "Y", "b": "X"}, state_family="X")
    timeline = MachineTimeline(M, table, available_from=0.0)
    placed = {"a": _place(timeline, _request("a", 10))}
    placed["c"] = _place(timeline, _request("c", 10, earliest=18))
    assert _times(placed["c"]) == (18, 23, 33)

    placed["b"] = _place(timeline, _request("b", 9))

    assert _times(placed["b"]) == (33, 38, 47)
    _assert_setups_match_start_order(table, placed)


@pytest.mark.parametrize("seed", range(20))
def test_random_insertions_keep_every_setup_equal_to_its_start_order_predecessor(
    seed: int,
) -> None:
    rng = np.random.default_rng(seed)
    families = {f"o{k}": str(rng.choice(["X", "Y"])) for k in range(15)}
    table = _table(families, state_family="X")
    timeline = MachineTimeline(M, table, available_from=0.0)
    placed: dict[str, Slot] = {}

    for op_id in families:
        earliest = float(rng.integers(0, 120))
        run = float(rng.integers(1, 12))
        placed[op_id] = _place(timeline, _request(op_id, run, earliest=earliest))

    _assert_setups_match_start_order(table, placed)


# --- calendars ---------------------------------------------------------------------------------


def test_forbid_jumps_to_the_first_working_interval_that_holds_the_whole_occupation() -> None:
    timeline = MachineTimeline(M, _table({"a": "Y"}, state_family="X"), available_from=0.0)
    calendar = ((0.0, 10.0), (20.0, 40.0))

    assert _times(timeline.find_slot(_request("a", 3, earliest=5, calendar=calendar))) == (
        20,
        25,
        28,
    )


def test_forbid_accepts_touching_intervals_as_one_working_run() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)
    calendar = ((0.0, 10.0), (10.0, 20.0))

    assert _times(timeline.find_slot(_request("a", 15, calendar=calendar))) == (0, 0, 15)


def test_forbid_starts_at_the_next_working_instant() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)

    assert _times(timeline.find_slot(_request("a", 2, earliest=12, calendar=((15.0, 20.0),)))) == (
        15,
        15,
        17,
    )


def test_pause_counts_setup_and_run_only_in_working_time() -> None:
    table = _table({"a": None}, function=SetupFunction(id="two", terms=(), default_seconds=2.0))
    timeline = MachineTimeline(M, table, available_from=0.0)
    calendar = ((0.0, 5.0), (10.0, 20.0))

    slot = timeline.find_slot(_request("a", 6, earliest=3, pause=True, calendar=calendar))

    assert _times(slot) == (3, 5, 16)
    assert slot is not None and slot.setup_seconds == 2.0


def test_pause_slot_must_still_end_before_the_next_occupation() -> None:
    timeline = MachineTimeline(M, _table({"a": None, "b": None}), available_from=0.0)
    calendar = ((0.0, 5.0), (10.0, 40.0))
    _place(timeline, _request("b", 5, earliest=10, calendar=calendar))

    slot = timeline.find_slot(_request("a", 6, pause=True, calendar=calendar))

    assert _times(slot) == (15, 15, 21)


def test_detached_pause_setup_never_ends_before_the_gate() -> None:
    table = _table({"a": None}, function=SetupFunction(id="four", terms=(), default_seconds=4.0))
    timeline = MachineTimeline(M, table, available_from=0.0)
    calendar = ((0.0, 5.0), (8.0, 30.0))

    slot = timeline.find_slot(
        _request("a", 3, gate=9, detached=True, pause=True, calendar=calendar)
    )

    assert slot is not None
    assert slot.setup_end >= 9
    assert slot.end == slot.setup_end + 3


def test_find_slot_returns_none_when_the_calendar_runs_out() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)

    assert timeline.find_slot(_request("a", 8, calendar=((0.0, 5.0),))) is None
    assert timeline.find_slot(_request("a", 8, pause=True, calendar=((0.0, 5.0),))) is None


# --- slot filter and placement -----------------------------------------------------------------


def test_slot_filter_retry_moves_the_search_forward() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)
    seen: list[float] = []

    def not_before_seven(slot: Slot) -> float | None:
        seen.append(slot.start)
        return None if slot.start >= 7 else 7.0

    slot = timeline.find_slot(_request("a", 2), not_before_seven)

    assert _times(slot) == (7, 7, 9)
    assert seen == [0, 7]


def test_slot_filter_that_does_not_move_forward_is_an_error() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)

    with pytest.raises(ValueError, match="retry"):
        timeline.find_slot(_request("a", 2), lambda slot: slot.start)


def test_place_rejects_an_overlapping_slot() -> None:
    timeline = MachineTimeline(M, _table({"a": None, "b": None}), available_from=0.0)
    _place(timeline, _request("a", 10))

    with pytest.raises(ValueError, match="overlaps"):
        timeline.place(_request("b", 2), Slot(start=5, setup_end=5, end=7, setup_seconds=0))


def test_place_returns_no_batch_id_on_a_unary_machine() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)
    request = _request("a", 2, family="F")
    slot = timeline.find_slot(request)
    assert slot is not None

    assert timeline.place(request, slot) is None


# --- PoolTimeline ------------------------------------------------------------------------------


def test_pool_blocked_returns_when_the_first_over_capacity_run_ends() -> None:
    pool = PoolTimeline("P", size=2)
    pool.hold(0, 10, 1)
    pool.hold(5, 15, 1)

    assert pool.blocked(2, 8, 1) == 10
    assert pool.blocked(2, 20, 1) == 10
    assert pool.blocked(10, 20, 1) is None
    assert pool.blocked(15, 20, 2) is None
    assert pool.blocked(3, 3, 5) is None
    assert pool.blocked(0, 1, 3) == INF


def test_needs_on_one_pool_and_phase_are_summed() -> None:
    pools = {"P": PoolTimeline("P", size=1)}
    needs = [PoolNeed("P", 1, "setup"), PoolNeed("P", 1, "both")]
    slot = Slot(start=0, setup_end=3, end=10, setup_seconds=3)

    assert operator_retry(slot, needs, pools) == INF


def test_find_slot_gives_up_when_the_filter_says_never() -> None:
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)

    assert timeline.find_slot(_request("a", 2), lambda slot: INF) is None


def test_pool_hold_rejects_an_overload() -> None:
    pool = PoolTimeline("P", size=1)
    pool.hold(0, 10, 1)

    with pytest.raises(ValueError, match="pool P"):
        pool.hold(5, 6, 1)


def _two_machines() -> tuple[MachineTimeline, MachineTimeline]:
    three = SetupFunction(id="three", terms=(), default_seconds=3.0)
    machine_a = MachineTimeline(M, _table({"a": None}, function=three), available_from=0.0)
    machine_b = MachineTimeline(M, _table({"b": None, "c": None}, function=None), available_from=0)
    return machine_a, machine_b


def _place_with_operators(
    timeline: MachineTimeline,
    request: SlotRequest,
    needs: Sequence[PoolNeed],
    pools: dict[str, PoolTimeline],
) -> Slot:
    slot = timeline.find_slot(request, lambda candidate: operator_retry(candidate, needs, pools))
    assert slot is not None
    timeline.place(request, slot)
    hold_operators(slot, needs, pools)
    return slot


def test_setup_only_need_does_not_block_the_pool_during_the_run() -> None:
    pools = {"P": PoolTimeline("P", size=1)}
    machine_a, machine_b = _two_machines()
    a = _place_with_operators(machine_a, _request("a", 7), [PoolNeed("P", 1, "setup")], pools)

    b = _place_with_operators(machine_b, _request("b", 5), [PoolNeed("P", 1, "run")], pools)

    assert _times(a) == (0, 3, 10)
    assert _times(b) == (3, 3, 8)


def test_both_phase_need_waits_until_the_pool_is_free() -> None:
    pools = {"P": PoolTimeline("P", size=1)}
    pools["P"].hold(0, 6, 1)
    _, machine_b = _two_machines()

    c = _place_with_operators(machine_b, _request("c", 2), [PoolNeed("P", 1, "both")], pools)

    assert _times(c) == (6, 6, 8)


def test_run_phase_conflict_moves_the_start_by_the_setup_offset() -> None:
    pools = {"P": PoolTimeline("P", size=1)}
    pools["P"].hold(0, 10, 1)
    machine_a, _ = _two_machines()

    a = _place_with_operators(machine_a, _request("a", 4), [PoolNeed("P", 1, "run")], pools)

    assert _times(a) == (7, 10, 14)


def test_operator_retry_is_none_without_needs() -> None:
    assert operator_retry(Slot(start=0, setup_end=1, end=2, setup_seconds=1), [], {}) is None


def test_hold_operators_holds_every_phase_window() -> None:
    pools = {"P": PoolTimeline("P", size=1), "Q": PoolTimeline("Q", size=1)}
    slot = Slot(start=0, setup_end=3, end=10, setup_seconds=3)

    hold_operators(slot, [PoolNeed("P", 1, "setup"), PoolNeed("Q", 1, "both")], pools)

    assert pools["P"].blocked(3, 10, 1) is None
    assert pools["P"].blocked(2, 3, 1) == 3
    assert pools["Q"].blocked(9, 10, 1) == 10


def test_hold_operators_holds_nothing_when_any_pool_would_overload() -> None:
    pools = {"P": PoolTimeline("P", size=1), "Q": PoolTimeline("Q", size=1)}
    pools["Q"].hold(0, 10, 1)
    slot = Slot(start=0, setup_end=3, end=10, setup_seconds=3)

    with pytest.raises(ValueError, match="pool Q"):
        hold_operators(slot, [PoolNeed("P", 1, "both"), PoolNeed("Q", 1, "run")], pools)
    assert pools["P"].blocked(0, 10, 1) is None


def test_pool_calendar_is_respected_through_the_effective_calendar() -> None:
    pool = PoolTimeline("P", size=1, calendar=((0.0, 20.0), (30.0, 60.0)))
    machine_calendar = ((0.0, 100.0),)
    calendar = effective_calendar(machine_calendar, [pool.calendar])
    timeline = MachineTimeline(M, _table({"a": None}), available_from=0.0)

    slot = timeline.find_slot(_request("a", 15, earliest=10, calendar=calendar))

    assert calendar == [(0.0, 20.0), (30.0, 60.0)]
    assert _times(slot) == (30, 30, 45)


def test_pause_with_a_pool_calendar_works_only_while_the_pool_works() -> None:
    pool = PoolTimeline("Q", size=1, calendar=((0.0, 5.0), (10.0, 20.0)))
    calendar = effective_calendar(None, [pool.calendar])
    timeline = MachineTimeline(M, _table({"d": None}), available_from=0.0)

    slot = timeline.find_slot(_request("d", 8, pause=True, calendar=calendar))

    assert _times(slot) == (0, 0, 13)


def test_effective_calendar_of_nothing_is_always_working() -> None:
    assert effective_calendar(None, []) is None


# --- batches -----------------------------------------------------------------------------------


def _oven(capacity: float = 10.0) -> MachineTimeline:
    table = _table(
        {op_id: None for op_id in ("a1", "a2", "a3", "a4", "a5")}, function=None, machine_id="O"
    )
    return MachineTimeline("O", table, available_from=0.0, batch_capacity=capacity)


def _batch_request(
    op_id: str, quantity: float, family: str, *, earliest: float = 0.0, gate: float = 0.0
) -> SlotRequest:
    return _request(op_id, 20, quantity=quantity, family=family, earliest=earliest, gate=gate)


def _place_batch(oven: MachineTimeline, request: SlotRequest) -> tuple[Slot, str | None]:
    slot = oven.find_slot(request)
    assert slot is not None
    return slot, oven.place(request, slot)


def test_ops_join_an_open_batch_of_the_same_family_until_capacity() -> None:
    oven = _oven()

    a1 = _place_batch(oven, _batch_request("a1", 4, "F"))
    a2 = _place_batch(oven, _batch_request("a2", 5, "F"))
    a3 = _place_batch(oven, _batch_request("a3", 3, "F"))
    a4 = _place_batch(oven, _batch_request("a4", 1, "G"))

    assert [(_times(slot), batch) for slot, batch in (a1, a2, a3, a4)] == [
        ((0, 0, 20), "O#b1"),
        ((0, 0, 20), "O#b1"),
        ((20, 20, 40), "O#b2"),
        ((40, 40, 60), "O#b3"),
    ]
    assert a2[0].batch_id == "O#b1"


def test_op_whose_gate_is_after_the_batch_start_opens_a_new_batch() -> None:
    oven = _oven()
    _place_batch(oven, _batch_request("a1", 4, "F"))

    slot, batch = _place_batch(oven, _batch_request("a2", 1, "F", gate=5))

    assert (_times(slot), batch) == ((20, 20, 40), "O#b2")


def test_op_with_a_later_earliest_start_does_not_join_an_earlier_batch() -> None:
    oven = _oven()
    _place_batch(oven, _batch_request("a1", 4, "F"))

    slot, batch = _place_batch(oven, _batch_request("a2", 1, "F", earliest=1))

    assert (_times(slot), batch) == ((20, 20, 40), "O#b2")


def test_op_with_a_different_run_length_does_not_join() -> None:
    oven = _oven()
    _place_batch(oven, _batch_request("a1", 4, "F"))

    request = _request("a2", 25, quantity=1, family="F")
    slot, batch = _place_batch(oven, request)

    assert (_times(slot), batch) == ((20, 20, 45), "O#b2")


def test_batch_join_honours_the_slot_filter() -> None:
    oven = _oven()
    _place_batch(oven, _batch_request("a1", 4, "F"))

    slot = oven.find_slot(_batch_request("a2", 1, "F"), lambda s: None if s.start >= 20 else 20.0)

    assert slot is not None
    assert (_times(slot), slot.batch_id) == ((20, 20, 40), None)


def test_batch_ids_are_deterministic() -> None:
    def run() -> list[str | None]:
        oven = _oven(capacity=5)
        return [
            _place_batch(oven, _batch_request(op_id, 3, "F"))[1]
            for op_id in ("a1", "a2", "a3", "a4", "a5")
        ]

    assert run() == run() == ["O#b1", "O#b2", "O#b3", "O#b4", "O#b5"]


def test_joining_over_capacity_by_place_is_an_error() -> None:
    oven = _oven(capacity=5)
    first, batch = _place_batch(oven, _batch_request("a1", 4, "F"))
    joined = Slot(
        start=first.start,
        setup_end=first.setup_end,
        end=first.end,
        setup_seconds=first.setup_seconds,
        batch_id=batch,
    )

    with pytest.raises(ValueError, match="capacity"):
        oven.place(_batch_request("a2", 3, "F"), joined)
