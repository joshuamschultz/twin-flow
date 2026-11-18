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

With `decisions=True` it also writes one `decision` move per dispatch decision (the
`PolicyRuntime` trace): actor = location, action = the chosen rule, `before` = the floor at
the decision instant and `after` = the floor at the next decision (or the end of the run).
Decision moves carry no dollars; the money sits on the period moves (COMP-153,
`twinflow.engines.des_ledger`), so summing a run's ledgers never counts a dollar twice.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from twinflow.engines.des_ledger import period_ledger
from twinflow.plan.observers import RunView
from twinflow.state.economics import Economics
from twinflow.state.issues import Issue, unknown_keys
from twinflow.state.recorder import StateRecorder
from twinflow.state.records import Ledger, Move, Snapshot

SECONDS_PER_HOUR = 3600.0
STATE_CAPTURE_FIELDS: tuple[str, ...] = ("every_hours", "decisions")


@dataclass(frozen=True)
class StateCapture:
    """The parsed `experiment.state_capture: {every_hours, decisions}` block (T-248)."""

    every_hours: float | None = None
    decisions: bool = False

    def observer(self, economics: Economics | None = None, *, run_id: str = "") -> DesStateObserver:
        """A fresh observer for one replication."""
        return DesStateObserver(
            self.every_hours, decisions=self.decisions, economics=economics, run_id=run_id
        )


def parse_state_capture(
    raw: object, path: str = "experiment.state_capture"
) -> tuple[StateCapture | None, list[Issue]]:
    """Read the block; problems are plain-word errors naming the field (REQ-213)."""
    fix = "Write state_capture: {every_hours: 4, decisions: true}."
    if not isinstance(raw, Mapping):
        return None, [Issue(path, "state_capture must be a set of named fields.", "error", fix)]
    issues = unknown_keys(dict(raw), STATE_CAPTURE_FIELDS, path)
    hours = raw.get("every_hours")
    if hours is not None:
        try:
            hours = _check_hours(hours)
        except ValueError:
            issues.append(
                Issue(
                    f"{path}.every_hours",
                    f"{hours!r} is not a positive number of hours.",
                    "error",
                    "Write a number above 0, like 4 or 0.5.",
                )
            )
            hours = None
    decisions = raw.get("decisions", False)
    if not isinstance(decisions, bool):
        issues.append(
            Issue(f"{path}.decisions", f"{decisions!r} is not true or false.", "error", fix)
        )
        decisions = False
    if issues:
        return None, issues
    if hours is None and not decisions:
        message = "state_capture records nothing: give every_hours, decisions: true, or both."
        return None, [Issue(path, message, "error", fix)]
    return StateCapture(hours, decisions), []


def _check_hours(every_hours: float | None) -> float | None:
    if every_hours is None:
        return None
    if (
        isinstance(every_hours, bool)
        or not isinstance(every_hours, int | float)
        or not math.isfinite(every_hours)
        or every_hours <= 0
    ):
        raise ValueError(f"every_hours must be a positive number of hours, got {every_hours!r}")
    return float(every_hours)


class DesStateObserver:
    """Samples the floor every `every_hours` hours and at each dispatch decision (REQ-197)."""

    def __init__(
        self,
        every_hours: float | None,
        *,
        decisions: bool = False,
        economics: Economics | None = None,
        run_id: str = "",
    ) -> None:
        self.every_hours = _check_hours(every_hours)
        if self.every_hours is None and not decisions:
            raise ValueError("state capture needs every_hours or decisions (or both)")
        self.decisions = decisions
        self.economics = economics if economics is not None else Economics()
        self._step = math.inf if self.every_hours is None else self.every_hours * SECONDS_PER_HOUR
        self._pending: tuple[Snapshot, str, str] | None = None
        self._decision_count = 0
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
        """Close the previous decision move at this instant and open a new one."""
        if not self.decisions:
            return
        snapshot = self.snapshot(time, self._decision_period())
        self._close_decision(snapshot)
        self._pending = (snapshot, location_id, rule)

    def finalize(self, run_dir: Path, termination_reason: str) -> None:
        """Sample the boundaries up to the end, one last sample at the end, then write."""
        del termination_reason
        end = self._require_view().now
        if self.every_hours is not None:
            while self._boundary() <= end:
                self._sample(self._boundary())
            if self._last is None or self._last.t < end:
                self._sample(end)
        if self._pending is not None:
            self._close_decision(self.snapshot(end, self._decision_period()))
        self._recorder.flush(run_dir)

    # -- sampling ------------------------------------------------------------------------------

    def _require_view(self) -> RunView:
        if self._view is None:
            raise RuntimeError("DesStateObserver used before start(view)")
        return self._view

    def _boundary(self) -> float:
        return self._next_period * self._step if self.every_hours is not None else math.inf

    def _decision_period(self) -> int:
        """The period a decision falls in: the last sample's, or the decision count."""
        if self.every_hours is None:
            self._decision_count += 1
            return self._decision_count - 1
        return max(self._next_period - 1, 0)

    def _close_decision(self, after: Snapshot) -> None:
        if self._pending is not None:
            before, actor, action = self._pending
            self._add(before, action, actor, "decision", after)
            self._pending = None

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
        ledger = Ledger()
        events: dict[str, float] = {}
        if kind == "period":
            orders = self._require_view().orders()
            ledger, events = period_ledger(before, after, orders, self.economics)
        move = Move(
            index=self._index,
            before=before,
            action=action,
            actor=actor,
            kind="decision" if kind == "decision" else "period",
            events=events,
            after=after,
            ledger=ledger,
        )
        self._index += 1
        self._recorder.add(move)
