"""COMP-152: plant-state capture for the discrete-event simulator (REQ-197, D-136).

`DesStateObserver` is a `twinflow.plan.observers.RunObserver`. Passed to
`RunDriver.run(..., observers=[...])`, it records `Snapshot`s of the live floor every
`every_hours` hours and writes one `period` move per interval to `moves.parquet` /
`states.parquet` in the run directory.

Sampling is lazy: the driver tells the observer before every SimPy step how far the clock
is about to jump (`on_advance(next_time)`). Between two events the floor does not change,
so the state at a sample time `b` is the state once every event at or before `b` has run.
The observer schedules no SimPy process of its own, so it can never keep a run alive past
its natural end, it stops exactly where the run stops (drain, natural end or the sim-time
limit), and the event logs are byte-identical to a run without it (D-136).

Snapshot fields: `t` is the sample time in seconds; `period` the sample number; `stock`
the stock levels; `setup` the setup group of every machine by machine id, busy or idle
(D-137); `queue` the jobs waiting per location; `wip` the jobs waiting plus being worked;
`open_orders` the released orders not yet complete; `labor` the free operators per pool;
`extra["busy.<location>"]` the jobs being worked per location.
"""

from __future__ import annotations

import math
from pathlib import Path

from twinflow.plan.observers import RunView
from twinflow.state.recorder import StateRecorder
from twinflow.state.records import Ledger, Move, Snapshot

SECONDS_PER_HOUR = 3600.0


class DesStateObserver:
    """Samples the floor every `every_hours` hours and writes period moves (REQ-197)."""

    def __init__(self, every_hours: float, *, run_id: str = "") -> None:
        if (
            isinstance(every_hours, bool)
            or not isinstance(every_hours, int | float)
            or not math.isfinite(every_hours)
            or every_hours <= 0
        ):
            raise ValueError(f"every_hours must be a positive number of hours, got {every_hours!r}")
        self.every_hours = float(every_hours)
        self._step = self.every_hours * SECONDS_PER_HOUR
        self._recorder = StateRecorder(run_id)
        self._view: RunView | None = None
        self._next_period = 0
        self._last: Snapshot | None = None
        self._index = 0

    @property
    def moves(self) -> int:
        """How many moves have been recorded so far."""
        return len(self._recorder)

    # -- RunObserver ---------------------------------------------------------------------------

    def start(self, view: RunView) -> None:
        self._view = view

    def on_advance(self, next_time: float) -> None:
        """Sample every boundary the clock is about to pass (state is constant until then)."""
        while self._boundary() < next_time:
            self._sample(self._boundary())

    def on_decision(self, location_id: str, time: float, rule: str) -> None:
        del location_id, time, rule

    def finalize(self, run_dir: Path, termination_reason: str) -> None:
        """Sample the boundaries up to the end, one last sample at the end, then write."""
        del termination_reason
        end = self._require_view().now
        while self._boundary() <= end:
            self._sample(self._boundary())
        if self._last is None or self._last.t < end:
            self._sample(end)
        self._recorder.flush(run_dir)

    # -- sampling ------------------------------------------------------------------------------

    def _require_view(self) -> RunView:
        if self._view is None:
            raise RuntimeError("DesStateObserver used before start(view)")
        return self._view

    def _boundary(self) -> float:
        return self._next_period * self._step

    def _sample(self, t: float) -> None:
        snapshot = self.snapshot(t, self._next_period)
        self._next_period += 1
        if self._last is not None:
            self._add(self._last, None, None, "period", snapshot)
        self._last = snapshot

    def snapshot(self, t: float, period: int) -> Snapshot:
        """The live floor as a `Snapshot` stamped with time `t` and `period`."""
        view = self._require_view()
        queue = view.queue_lengths()
        busy = view.in_flight()
        return Snapshot(
            t=t,
            period=period,
            stock=view.stock_levels(),
            setup=view.setups(),
            queue=queue,
            wip=sum(queue.values()) + sum(busy.values()),
            open_orders=view.open_orders(),
            labor=view.labor_available(),
            extra={f"busy.{location}": float(count) for location, count in busy.items()},
        )

    def _add(
        self,
        before: Snapshot,
        action: str | None,
        actor: str | None,
        kind: str,
        after: Snapshot,
    ) -> None:
        move = Move(
            index=self._index,
            before=before,
            action=action,
            actor=actor,
            kind="decision" if kind == "decision" else "period",
            events={},
            after=after,
            ledger=Ledger(),
        )
        self._index += 1
        self._recorder.add(move)
