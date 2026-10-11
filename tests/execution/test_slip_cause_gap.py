"""Slip cause when no constraint time is at or before the start (D-098 gap rule).

Orchestrator decision (TF-004, T-181): a late op with no constraint time at or before its
start gets the cause of the latest constraint time it has. With no constraint time at all
its gate never opened, which in a record means unfinished predecessors: `"precedence"`.
"""

from __future__ import annotations

from twinflow.execution import metrics
from twinflow.execution.metrics import OpRecord


def _late(**times: float | None) -> OpRecord:
    """An op planned at 10 that started at 20; every constraint time unset unless given."""
    fields: dict[str, float | None] = {
        "gate_time": None,
        "machine_free_time": None,
        "operator_time": None,
        "calendar_time": None,
        "machine_up_time": None,
    } | times
    return OpRecord(
        operation_id="o",
        job_id="J",
        machine_id="M1",
        batch_id=None,
        planned_start=10.0,
        planned_end=20.0,
        start=20.0,
        setup_end=20.0,
        end=30.0,
        setup_seconds=0.0,
        run_seconds=10.0,
        interrupted_seconds=0.0,
        overrun_seconds=0.0,
        max_lag_violation_seconds=0.0,
        pin_window_missed=False,
        censored=False,
        **fields,
    )


def test_every_constraint_after_the_start_gives_the_latest_ones_cause() -> None:
    record = _late(gate_time=25.0, machine_free_time=30.0)

    assert metrics.slip_cause(record) == "machine"


def test_latest_after_start_constraint_ties_go_to_the_earlier_wait_stage() -> None:
    record = _late(gate_time=30.0, machine_free_time=30.0)

    assert metrics.slip_cause(record) == "precedence"


def test_no_constraint_time_at_all_means_the_gate_never_opened() -> None:
    # A never-started op whose predecessor never finished: every time is None.
    assert metrics.slip_cause(_late()) == "precedence"
