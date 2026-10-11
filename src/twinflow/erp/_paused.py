"""In-process ops whose crew is off shift at `as_of` are paused at now (TF-005 ruling B, DEV-12).

An ERP in-process op with a crew cannot be running at `as_of` when nobody of its labor pool
is at work then: it is paused, and resumes on its machine when the crew comes back. The
builder emits it as a PENDING op instead of an in-progress one:

* crew off shift = the pool's calendar has no working time at `as_of` (working intervals
  are `[start, end)`; a pool with no calendar is always on shift);
* ONE option, on its running machine (explicit or inferred, `_jobs.running_machines`), with
  `fixed_seconds` = the remaining run time and `unit_seconds = 0`, `ready_time = 0`; it keeps
  its crew needs and never joins a batch (a half-run load takes no new parts);
* one hard `LockOverride` (machine, no start) per paused op, so the plan keeps it there;
* its machine is not blocked: a pending op adds nothing to `available_from`.

An op whose crew is on shift at `as_of` stays in progress, unchanged.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Iterable, Mapping
from zoneinfo import ZoneInfo

from twinflow.erp.calendars import expand
from twinflow.erp.schema import ErpSnapshot
from twinflow.scheduling.model import LockOverride, OperationStatus, ProcessingOption

__all__ = ["OpKey", "off_shift_pools", "paused_locks", "paused_option"]

OpKey = tuple[str, str]
"""(order id, op id) of an order operation."""

# Working time is looked up just past `as_of`; one day covers any window that is open then.
_PROBE = dt.timedelta(days=1)

_REASON = (
    "in-process operation paused: its crew is off shift at as_of, so it resumes on the "
    "machine it is on when the crew returns"
)


def off_shift_pools(snapshot: ErpSnapshot) -> frozenset[str]:
    """Labor pool ids whose calendar has no working time at `as_of` (first record wins)."""
    tz = ZoneInfo(snapshot.plant_timezone)
    calendars = {calendar.id: calendar for calendar in reversed(snapshot.calendars)}
    pools = {pool.id: pool.calendar_id for pool in reversed(snapshot.labor_pools)}
    off: set[str] = set()
    for pool_id, calendar_id in pools.items():
        calendar = calendars.get(calendar_id) if calendar_id is not None else None
        if calendar is None:
            continue  # no calendar: always on shift
        intervals = expand(calendar, tz, snapshot.as_of, snapshot.as_of + _PROBE)
        if not intervals or intervals[0][0] > 0.0:
            off.add(pool_id)
    return frozenset(off)


def paused_option(
    options: tuple[ProcessingOption, ...], status: OperationStatus
) -> tuple[ProcessingOption, ...]:
    """The one option of a paused op: its running machine, the remaining time as fixed time."""
    option = next(found for found in options if found.machine_id == status.machine_id)
    remaining = status.remaining_seconds or 0.0
    return (option.model_copy(update={"fixed_seconds": remaining, "unit_seconds": 0.0}),)


def paused_locks(
    paused: Iterable[OpKey], machines: Mapping[OpKey, str | None]
) -> tuple[LockOverride, ...]:
    """One hard lock per paused op onto its machine, sorted by operation id."""
    locks = []
    for order_id, op_id in sorted(paused):
        operation_id = f"{order_id}:{op_id}"
        locks.append(
            LockOverride(
                id="erp-paused-" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24],
                hardness="hard",
                operation_id=operation_id,
                machine_id=machines[(order_id, op_id)],
                start=None,
                fact_ref=f"erp in_process_paused_offshift: {order_id} op {op_id}",
                reason=_REASON,
            )
        )
    return tuple(locks)
