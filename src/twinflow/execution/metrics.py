"""Per-operation execution records (COMP-136, D-097, D-098).

`OpRecord` is the one interface between a run and every metric (the D-017 rule): the
executor writes one per pending operation, and replication metrics (T-181) read only
these. It is a slotted frozen dataclass built once per operation at the end of a run, so
a 5,000-operation replication pays no validation cost.

Times are problem seconds. A censored operation (the run stopped before it finished) has
`end` at the run's end time; one that never started also has `start` and `setup_end`
there and zero drawn seconds.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OpRecord:
    """What happened to one operation in one replication.

    The binding-constraint times feed the slip cause (D-098, filled by metrics):
    `gate_time` (material gate: release, ready time, every predecessor link; None when it
    never opened), `machine_free_time` (previous unit's end, else max(now,
    available_from)), `operator_time` (latest operator grant; None without operator
    needs), `calendar_time` (earliest start the calendar rule allowed; None without a
    calendar) and `machine_up_time` (end of the outage that held the start back; None when
    none did). `setup_seconds` and `run_seconds` are drawn work (planned x factor) and
    exclude downtime; `interrupted_seconds` is the downtime that stopped its work.
    """

    operation_id: str
    job_id: str
    machine_id: str
    batch_id: str | None
    planned_start: float | None
    planned_end: float | None
    gate_time: float | None
    machine_free_time: float | None
    operator_time: float | None
    calendar_time: float | None
    machine_up_time: float | None
    start: float
    setup_end: float
    end: float
    setup_seconds: float
    run_seconds: float
    interrupted_seconds: float
    overrun_seconds: float
    max_lag_violation_seconds: float
    pin_window_missed: bool
    censored: bool
    slip_cause: str | None = None


__all__ = ["OpRecord"]
