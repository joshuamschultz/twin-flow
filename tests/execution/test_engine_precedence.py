"""T-177 (RED): lags, max lags, transfer-lot overlap, pins and the horizon in execution.

REQ-130, REQ-131, COMP-135, COMP-136, D-090, D-091. Executor contract: test_fidelity.py.

API beyond the test_fidelity.py contract, fixed by these tests:

- `ExecutionRun.horizon_violations: tuple[str, ...]`: ids of the operations whose actual
  end is after the compiled `horizon_end` (+ verifier TOLERANCE), in record order; `()`
  when no horizon is set or none exceeds it (D-090: recorded, not enforced). Chosen over a
  new `OpRecord` field so the COMP-136 record stays as specified.
- `OpRecord.gate_time`: when the operation's material gate opened (release, ready time and
  every precedence link, an overlap link at the predecessor's transfer instant).
- `OpRecord.max_lag_violation_seconds`: the largest `g - (pred.end + max_lag)` over its
  links (`g` = start, or setup end when detached; overlap links measure from the transfer
  instant), 0 when none is broken.
- `OpRecord.pin_window_missed`: True when the actual start is after the pin's
  `latest_start` (+ TOLERANCE).
- `OpRecord.interrupted_seconds`: seconds the operation's machine was down while it was in
  progress (outages; work resumes after them).

Times are computed by hand in each test. Factor stubs (`StubSource`) make an operation
slip; scripted outages pass through `execute(..., outages=...)` with probability 1.
"""

from __future__ import annotations

import pytest
from _handplans import (
    EXACT,
    StubSource,
    a,
    assert_replays,
    hand_plan,
    op,
    option,
    records,
    request,
    run_plan,
    verified_plan,
)

from twinflow.execution.policy import ScriptedOutage

CENTERS = [{"id": "W"}]


def _machines(*calendar_ids: str | None) -> list[dict[str, object]]:
    machines: list[dict[str, object]] = []
    for k, calendar_id in enumerate(calendar_ids, start=1):
        machine: dict[str, object] = {"id": f"M{k}", "work_center_id": "W"}
        if calendar_id is not None:
            machine["calendar_id"] = calendar_id
        machines.append(machine)
    return machines


def _two_step(link: dict[str, object], a_fixed: float = 10.0, b_fixed: float = 5.0) -> list:
    """Job J: A on M1 then B on M2 joined by `link` (A's id is filled in)."""
    first = op("A", "W", [option("M1", a_fixed)])
    second = op("B", "W", [option("M2", b_fixed)], predecessors=[{"operation_id": "A", **link}])
    return [{"id": "J", "operations": [first, second]}]


# --- min lag ---------------------------------------------------------------------------------


def test_min_lag_plan_replays_exactly() -> None:
    req = request(_two_step({"min_lag": 7.0}), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 10), a("B", "M2", 17, 22)])

    assert_replays(run_plan(compiled, plan), plan)


def test_min_lag_delays_the_gate_after_a_predecessor_slip() -> None:
    req = request(_two_step({"min_lag": 7.0}), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 10), a("B", "M2", 17, 22)])

    run = run_plan(compiled, plan, source=StubSource(run={"M1": 2.0}))

    found = records(run)
    assert found["A"].end == pytest.approx(20.0, abs=EXACT)
    assert found["B"].gate_time == pytest.approx(27.0, abs=EXACT)
    assert found["B"].start == pytest.approx(27.0, abs=EXACT)
    assert found["B"].end == pytest.approx(32.0, abs=EXACT)


# --- transfer-lot overlap (D-091) ------------------------------------------------------------


def _overlap_jobs(
    a_crossing: str = "forbid", b_fixed: float = 1.0, b_unit: float = 3.0, quantity: float = 4.0
) -> list:
    """A: q x (fixed 2, unit 5) on M1; B: q x (b_fixed, b_unit) on M2; transfer lot of 1."""
    first = op("A", "W", [option("M1", 2.0, 5.0)], quantity, shift_crossing=a_crossing)
    second = op(
        "B",
        "W",
        [option("M2", b_fixed, b_unit)],
        quantity,
        predecessors=[{"operation_id": "A", "overlap_quantity": 1.0}],
    )
    return [{"id": "J", "operations": [first, second]}]


def test_overlap_plan_replays_exactly() -> None:
    # A run 2 + 4 x 5 = 22; transfer at 0 + 2 + 5 = 7; B run 1 + 4 x 3 = 13 ends 25 >= 22 + 3.
    req = request(_overlap_jobs(), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 22), a("B", "M2", 12, 25)])

    assert_replays(run_plan(compiled, plan), plan)


def test_overlap_gate_follows_the_predecessors_run_work_and_the_last_lot_bounds_the_end() -> None:
    req = request(_overlap_jobs(), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 22), a("B", "M2", 12, 25)])

    run = run_plan(compiled, plan, source=StubSource(run={"M1": 2.0, "M2": 1.5}))

    found = records(run)
    # A at factor 2: run 44, its work reaches fixed + q x unit = 7 planned s at t = 14.
    assert found["A"].end == pytest.approx(44.0, abs=EXACT)
    assert found["B"].gate_time == pytest.approx(14.0, abs=EXACT)
    assert found["B"].start == pytest.approx(14.0, abs=EXACT)
    # B's own run ends at 14 + 13 x 1.5 = 33.5, but the last lot needs 44 + 1 x 3 x 1.5.
    assert found["B"].end == pytest.approx(48.5, abs=EXACT)


def test_overlap_gate_counts_only_worked_time_when_the_predecessor_pauses_over_a_break() -> None:
    # M1 works [0, 5) and [15, 1000). A (pause) runs 2 + 10 x 5 = 52 work s: 5 by t = 5, the
    # rest from 15, so it ends 62 and its work reaches 7 at t = 17 (not at clock 7, D-091).
    # The plan is hand-made with B planned at 0: plan control treats it as an earliest start.
    calendar = {"id": "CAL", "intervals": [(0.0, 5.0), (15.0, 1000.0)]}
    jobs = _overlap_jobs(a_crossing="pause", b_fixed=0.0, b_unit=6.0, quantity=10.0)
    req = request(jobs, _machines("CAL", None), CENTERS, calendars=[calendar])
    compiled, plan = hand_plan(req, [a("A", "M1", 0, 62), a("B", "M2", 0, 60)])

    found = records(run_plan(compiled, plan))

    assert found["A"].end == pytest.approx(62.0, abs=EXACT)
    assert found["B"].gate_time == pytest.approx(17.0, abs=EXACT)
    assert found["B"].start == pytest.approx(17.0, abs=EXACT)
    assert found["B"].end == pytest.approx(77.0, abs=EXACT)  # 17 + 60 >= 62 + 6


def test_overlap_gate_counts_only_worked_time_when_the_predecessor_is_interrupted() -> None:
    # A: 52 s on M1; B: 60 s on M2 planned at 8 (transfer 7, end 68 >= 52 + 6).
    jobs = _overlap_jobs(b_fixed=0.0, b_unit=6.0, quantity=10.0)
    req = request(jobs, _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 52), a("B", "M2", 8, 68)])
    outage = ScriptedOutage(machine_id="M1", start=3.0, duration=10.0)

    found = records(run_plan(compiled, plan, outages=[outage]))

    # A works 3 s, is down [3, 13), and reaches 7 work s at 17; it ends 52 + 10 = 62.
    assert found["A"].interrupted_seconds == pytest.approx(10.0, abs=EXACT)
    assert found["A"].end == pytest.approx(62.0, abs=EXACT)
    assert found["B"].gate_time == pytest.approx(17.0, abs=EXACT)
    assert found["B"].start == pytest.approx(17.0, abs=EXACT)
    assert found["B"].end == pytest.approx(77.0, abs=EXACT)


# --- max lag, pins, horizon: recorded, never enforced (D-090) ----------------------------


def test_max_lag_slip_is_recorded_and_does_not_block() -> None:
    req = request(_two_step({"max_lag": 5.0}), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 10), a("B", "M2", 10, 15)])
    outage = ScriptedOutage(machine_id="M2", start=10.0, duration=20.0)

    run = run_plan(compiled, plan, outages=[outage])

    found = records(run)
    assert run.termination == "completed"
    assert not found["B"].censored
    assert found["B"].start == pytest.approx(30.0, abs=EXACT)
    assert found["B"].end == pytest.approx(35.0, abs=EXACT)
    assert found["B"].max_lag_violation_seconds == pytest.approx(15.0, abs=EXACT)  # 30 - (10 + 5)
    assert found["A"].max_lag_violation_seconds == 0.0


def test_max_lag_kept_records_no_violation() -> None:
    req = request(_two_step({"max_lag": 5.0}), _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 10), a("B", "M2", 10, 15)])

    found = records(run_plan(compiled, plan))

    assert found["B"].max_lag_violation_seconds == 0.0


def test_missed_pin_latest_start_is_recorded() -> None:
    jobs = [
        {"id": "JP", "operations": [op("P", "W", [option("M1", 10.0)], pin={"latest_start": 5.0})]},
        {"id": "JQ", "operations": [op("Q", "W", [option("M2", 10.0)], pin={"latest_start": 5.0})]},
    ]
    req = request(jobs, _machines(None, None), CENTERS)
    compiled, plan = verified_plan(req, [a("P", "M1", 0, 10), a("Q", "M2", 0, 10)])
    outage = ScriptedOutage(machine_id="M1", start=0.0, duration=20.0)

    run = run_plan(compiled, plan, outages=[outage])

    found = records(run)
    assert run.termination == "completed"
    assert found["P"].start == pytest.approx(20.0, abs=EXACT)
    assert found["P"].pin_window_missed is True
    assert found["Q"].pin_window_missed is False


def test_exceeded_horizon_is_recorded_per_operation_and_does_not_block() -> None:
    jobs = [
        {"id": "JA", "operations": [op("A", "W", [option("M1", 20.0)])]},
        {"id": "JC", "operations": [op("C", "W", [option("M2", 10.0)])]},
    ]
    req = request(jobs, _machines(None, None), CENTERS, horizon_end=30.0)
    compiled, plan = verified_plan(req, [a("A", "M1", 0, 20), a("C", "M2", 0, 10)])

    on_plan = run_plan(compiled, plan)
    slipped = run_plan(compiled, plan, source=StubSource(run={"M1": 2.0}))

    assert on_plan.horizon_violations == ()
    assert slipped.termination == "completed"
    assert records(slipped)["A"].end == pytest.approx(40.0, abs=EXACT)
    assert slipped.horizon_violations == ("A",)


# --- former guard: one firing per piece and stage ------------------------------------------


def _diamond() -> list:
    """One job of four lots: a forks to b and c (min lag 1), both join at d."""
    lots = [
        op("a", "W", [option("M1", 2.0, 1.0)], 2.0),
        op("b", "W", [option("M2", 6.0)], predecessors=[{"operation_id": "a"}]),
        op("c", "W", [option("M3", 5.0)], predecessors=[{"operation_id": "a", "min_lag": 1.0}]),
        op(
            "d",
            "W",
            [option("M1", 3.0)],
            predecessors=[{"operation_id": "b"}, {"operation_id": "c"}],
        ),
    ]
    return [{"id": "J", "operations": lots}]


DIAMOND_PLAN = [
    a("a", "M1", 0, 4),
    a("b", "M2", 4, 10),
    a("c", "M3", 5, 10),
    a("d", "M1", 10, 13),
]


def test_former_guard_one_firing_per_piece_and_stage_lot_dag_replays_exactly() -> None:
    req = request(_diamond(), _machines(None, None, None), CENTERS)
    compiled, plan = verified_plan(req, DIAMOND_PLAN)

    run = run_plan(compiled, plan)

    assert_replays(run, plan)
    assert len(run.records) == 4


def test_former_guard_lot_dag_join_waits_for_the_slower_branch() -> None:
    req = request(_diamond(), _machines(None, None, None), CENTERS)
    compiled, plan = verified_plan(req, DIAMOND_PLAN)

    found = records(run_plan(compiled, plan, source=StubSource(run={"M3": 3.0})))

    assert found["b"].end == pytest.approx(10.0, abs=EXACT)
    assert found["c"].end == pytest.approx(20.0, abs=EXACT)  # 5 + 5 x 3
    assert found["d"].gate_time == pytest.approx(20.0, abs=EXACT)
    assert found["d"].start == pytest.approx(20.0, abs=EXACT)
    assert found["d"].end == pytest.approx(23.0, abs=EXACT)
