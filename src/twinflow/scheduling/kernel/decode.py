"""Decoder: machine choice + per-machine sequences -> schedule (COMP-004, REQ-089, REQ-107, D-071).

`decode(index, choice, sequences)` places every scheduled op of `index` on its chosen
machine, in the order its machine's sequence gives, and returns a `Decoded` or one named
`ScheduleIssue`. Every decoder-backed method returns its answer through this function, so
every TF-002 rule is honored in one place: the timelines and the shared precedence gate
(`timeline.material_gate`, `timeline.SlotCheck`, the same ones the baseline uses).

1. In-progress ops first, on their status machine at `now` for `remaining_seconds` with
   setup 0 (each must be first in its machine's sequence).
2. Every other op in a topological order of job arcs plus machine-sequence arcs (Kahn's
   order, a heap keyed by (gate ready, position) so ops reach operator pools roughly in
   time order). Ops left unplaced when the heap runs dry sit on a cycle: `sequence_cycle`,
   found without looping.
3. Each op is placed append-only after its machine predecessor: `earliest_start` is at
   least the predecessor's start, so the timeline never back-fills a gap before it (the
   sequence is the decision) and an op can join only the machine's last batch, the one
   its predecessor is in. Within that, the earliest slot on the effective calendar
   (forbid / pause) that meets the material gate, transfer-lot end floors, pin window and
   operator capacity; restart warm-ups come from the timeline.

An op with no slot returns `max_lag_unmet` (a max lag closed the window) or
`no_feasible_slot`; an op ending after `horizon_end` returns `horizon_exceeded`.
Malformed input (an op listed twice or not at all, an op sequenced on a machine other than
its choice, a choice that is not an eligible option, an op sequenced before an in-progress
op) raises ValueError: it is a method bug, not a plan issue.

`Decoded` keeps plain per-position tuples (no Pydantic object until `schedule()`), so the
local search can decode thousands of times.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.kernel.index import Link, MachineData, OpData, OpOption, ProblemIndex
from twinflow.scheduling.model import Assignment, Schedule, ScheduleIssue
from twinflow.scheduling.timeline import (
    Gate,
    MachineTimeline,
    Placed,
    PoolNeed,
    PoolTimeline,
    Slot,
    SlotCheck,
    SlotRequest,
    effective_calendar,
    hold_operators,
    material_gate,
    operator_retry,
)

ScheduleStatus = Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
_SLACK = 1e-9
"""Float slack on the horizon bound; far below the verifier's 1e-6."""
_NONE = -1


@dataclass(frozen=True, slots=True)
class Decoded:
    """A decoded schedule as per-op-position tuples over `index.ops`."""

    index: ProblemIndex
    options: tuple[OpOption, ...]
    starts: tuple[float, ...]
    setup_ends: tuple[float, ...]
    ends: tuple[float, ...]
    batch_ids: tuple[str | None, ...]

    def start(self, op_id: str) -> float:
        return self.starts[self.index.pos[op_id]]

    def setup_end(self, op_id: str) -> float:
        return self.setup_ends[self.index.pos[op_id]]

    def end(self, op_id: str) -> float:
        return self.ends[self.index.pos[op_id]]

    def machine(self, op_id: str) -> str:
        return self.options[self.index.pos[op_id]].machine_id

    def completion(self, job_id: str) -> float:
        """C_j: the latest end among the job's scheduled ops."""
        job = self.index.jobs[self.index.job_pos[job_id]]
        return max(self.ends[p] for p in job.ops)

    def schedule(self, solver: str, status: ScheduleStatus = "FEASIBLE") -> Schedule:
        """The decoded plan as a `Schedule` (assignments in op position order)."""
        assignments = tuple(
            Assignment(
                operation_id=op_id,
                machine_id=self.options[p].machine_id,
                start=self.starts[p],
                setup_end=self.setup_ends[p],
                end=self.ends[p],
                batch_id=self.batch_ids[p],
            )
            for p, op_id in enumerate(self.index.op_ids)
        )
        return Schedule(status=status, assignments=assignments, solver=solver, elapsed_seconds=0.0)


def decode(
    index: ProblemIndex, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> Decoded | ScheduleIssue:
    """Place every op of `index` per `choice` and `sequences` (see the module docstring)."""
    options = _chosen_options(index, choice)
    previous = _machine_predecessors(index, choice, sequences)
    return _Decoder(index, options, previous).run()


# --- input checks ----------------------------------------------------------------------------


def _chosen_options(index: ProblemIndex, choice: Mapping[str, str]) -> tuple[OpOption, ...]:
    """The chosen eligible option per op position; ValueError when missing or ineligible."""
    chosen: list[OpOption] = []
    for op in index.ops:
        machine_id = choice.get(op.id)
        option = next((o for o in op.options if o.machine_id == machine_id), None)
        if option is None:
            raise ValueError(f"{op.id}: choice {machine_id!r} is not an eligible option")
        chosen.append(option)
    return tuple(chosen)


def _machine_predecessors(
    index: ProblemIndex, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> list[int]:
    """Per op position, its machine predecessor's position (or `_NONE`).

    ValueError when an op is unknown, sequenced twice, sequenced off its chosen machine or
    left out of every sequence.
    """
    previous = [_NONE] * len(index.ops)
    seen = [False] * len(index.ops)
    for machine_id, sequence in sequences.items():
        last = _NONE
        for op_id in sequence:
            p = index.pos.get(op_id)
            if p is None or seen[p] or choice.get(op_id) != machine_id:
                raise ValueError(f"{op_id}: unknown, sequenced twice or off its choice")
            seen[p] = True
            previous[p] = last
            last = p
    missing = [index.op_ids[p] for p, flag in enumerate(seen) if not flag]
    if missing:
        raise ValueError(f"ops in no sequence: {missing}")
    return previous


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path, message=message)


# --- the decoder -----------------------------------------------------------------------------


class _Decoder:
    """Mutable state of one decode: timelines, pools and per-position results."""

    def __init__(
        self, index: ProblemIndex, options: tuple[OpOption, ...], previous: list[int]
    ) -> None:
        n = len(index.ops)
        self._index = index
        self._options = options
        self._previous = previous
        self._next = [_NONE] * n
        for p, prev in enumerate(previous):
            if prev != _NONE:
                self._next[prev] = p
        self._timelines = {m.id: _timeline(m, index) for m in index.machines}
        self._pools = {pid: PoolTimeline(pid, p.size, p.calendar) for pid, p in index.pools.items()}
        self._machine_calendar = {m.id: m.calendar for m in index.machines}
        self._calendars: dict[tuple[str, tuple[str, ...]], Intervals] = {}
        self._placed: list[Placed | None] = [None] * n
        self._starts = [0.0] * n
        self._setup_ends = [0.0] * n
        self._ends = [0.0] * n
        self._batch_ids: list[str | None] = [None] * n

    def run(self) -> Decoded | ScheduleIssue:
        issue = self._start_in_progress() or self._place_in_order()
        if issue is not None:
            return issue
        return Decoded(
            index=self._index,
            options=self._options,
            starts=tuple(self._starts),
            setup_ends=tuple(self._setup_ends),
            ends=tuple(self._ends),
            batch_ids=tuple(self._batch_ids),
        )

    # --- in-progress ---------------------------------------------------------------------

    def _start_in_progress(self) -> ScheduleIssue | None:
        """Place in-progress ops by id on [now, now + remaining) with setup 0."""
        running = [p for p, op in enumerate(self._index.ops) if op.in_progress is not None]
        for p in sorted(running, key=lambda p: self._index.op_ids[p]):
            issue = self._run_from_now(p)
            if issue is not None:
                return issue
        return None

    def _run_from_now(self, p: int) -> ScheduleIssue | None:
        op, now = self._index.ops[p], self._index.now
        prev = self._previous[p]
        if prev != _NONE and self._index.ops[prev].in_progress is None:
            raise ValueError(f"{op.id}: in progress but sequenced after {self._index.op_ids[prev]}")
        if prev != _NONE:
            return _issue("no_feasible_slot", op.id, f"{op.id}: its machine already runs an op")
        remaining = op.in_progress.remaining_seconds if op.in_progress else None
        slot = Slot(start=now, setup_end=now, end=now + (remaining or 0.0), setup_seconds=0.0)
        if op.operator_needs and operator_retry(slot, op.operator_needs, self._pools) is not None:
            return _issue("no_feasible_slot", op.id, f"{op.id}: operators busy at now")
        issue = self._horizon_issue(op.id, slot)
        if issue is not None:
            return issue
        request = SlotRequest(
            op_id=op.id,
            run_seconds=slot.end - now,
            earliest_start=now,
            gate_ready=now,
            quantity=op.quantity,
        )
        self._commit(p, request, slot)
        return None

    # --- topological placement -----------------------------------------------------------

    def _place_in_order(self) -> ScheduleIssue | None:
        """Kahn's order over job and machine arcs; `sequence_cycle` when ops are left over."""
        index = self._index
        waiting = [len(op.predecessors) for op in index.ops]
        for p, prev in enumerate(self._previous):
            waiting[p] += prev != _NONE
        heap = [
            (self._ready(p), p)
            for p, count in enumerate(waiting)
            if count == 0 and self._placed[p] is None
        ]
        heapq.heapify(heap)
        for p in range(len(index.ops)):
            if self._placed[p] is not None:
                self._release(p, waiting, heap)
        while heap:
            _, p = heapq.heappop(heap)
            issue = self._place(p)
            if issue is not None:
                return issue
            self._release(p, waiting, heap)
        if any(placed is None for placed in self._placed):
            return _issue(
                "sequence_cycle", "sequences", "job and machine-sequence arcs form a cycle"
            )
        return None

    def _release(self, p: int, waiting: list[int], heap: list[tuple[float, int]]) -> None:
        """Count `p` as placed for its job successors and machine follower."""
        followers = list(self._index.successors[p])
        if self._next[p] != _NONE:
            followers.append(self._next[p])
        for s in followers:
            waiting[s] -= 1
            if waiting[s] == 0 and self._placed[s] is None:
                heapq.heappush(heap, (self._ready(s), s))

    def _ready(self, p: int) -> float:
        """Heap key: the op's gate, or its machine predecessor's end when later."""
        prev = self._previous[p]
        ready = self._gate(self._index.ops[p]).ready
        return max(ready, self._ends[prev]) if prev != _NONE else ready

    def _gate(self, op: OpData) -> Gate:
        """Material gate from the placed predecessors (Kahn's order placed them all)."""
        pairs: list[tuple[Link, Placed]] = []
        for link in op.predecessors:
            pred = self._placed[link.pred_pos]
            if pred is None:
                raise AssertionError(f"{op.id}: predecessor {link.pred_id} not placed yet")
            pairs.append((link, pred))
        return material_gate(op.release, pairs)

    # --- one op --------------------------------------------------------------------------

    def _place(self, p: int) -> ScheduleIssue | None:
        op, option = self._index.ops[p], self._options[p]
        gate = self._gate(op)
        pin = op.pin
        request = self._request(p, op, option, gate)
        check = SlotCheck(
            gate,
            unit=option.unit_seconds,
            latest_start=pin.latest_start if pin else None,
            detached=op.setup_detached,
            needs=op.operator_needs,
            pools=self._pools,
        )
        slot = self._timelines[option.machine_id].find_slot(request, check)
        if slot is None:
            return _unplaceable(op.id, check.missed_max_lag)
        issue = self._horizon_issue(op.id, slot)
        if issue is None:
            self._commit(p, request, slot)
        return issue

    def _request(self, p: int, op: OpData, option: OpOption, gate: Gate) -> SlotRequest:
        """Slot request whose earliest start keeps the op after its machine predecessor."""
        earliest = self._index.now
        if op.pin is not None and op.pin.earliest_start is not None:
            earliest = max(earliest, op.pin.earliest_start)
        prev = self._previous[p]
        if prev != _NONE:
            earliest = max(earliest, self._starts[prev])
        return SlotRequest(
            op_id=op.id,
            run_seconds=option.run,
            earliest_start=earliest,
            gate_ready=gate.ready,
            quantity=op.quantity,
            calendar=self._calendar(option.machine_id, op.operator_needs),
            detached=op.setup_detached,
            shift_crossing=op.shift_crossing,
            batch_family=op.batch_family,
        )

    def _calendar(self, machine_id: str, needs: Sequence[PoolNeed]) -> Intervals:
        """Effective calendar: machine calendar intersected with each needed pool's (memoized)."""
        key = (machine_id, tuple(need.pool_id for need in needs))
        if key not in self._calendars:
            pool_calendars = (self._pools[pool_id].calendar for pool_id in key[1])
            self._calendars[key] = effective_calendar(
                self._machine_calendar[machine_id], pool_calendars
            )
        return self._calendars[key]

    def _horizon_issue(self, op_id: str, slot: Slot) -> ScheduleIssue | None:
        horizon = self._index.horizon_end
        if horizon is None or slot.end <= horizon + _SLACK:
            return None
        message = f"{op_id}: ends at {slot.end:g}, after horizon_end {horizon:g}"
        return _issue("horizon_exceeded", op_id, message)

    def _commit(self, p: int, request: SlotRequest, slot: Slot) -> None:
        """Record `slot` for op `p` on its machine and operator pools."""
        op, option = self._index.ops[p], self._options[p]
        self._batch_ids[p] = self._timelines[option.machine_id].place(request, slot)
        if op.operator_needs:
            hold_operators(slot, op.operator_needs, self._pools)
        self._placed[p] = Placed(
            slot.setup_end, slot.end, option.fixed_seconds, option.unit_seconds
        )
        self._starts[p] = slot.start
        self._setup_ends[p] = slot.setup_end
        self._ends[p] = slot.end


def _timeline(machine: MachineData, index: ProblemIndex) -> MachineTimeline:
    restart = machine.restart
    return MachineTimeline(
        machine.id,
        index.setups,
        available_from=machine.available_from,
        batch_capacity=machine.batch_capacity,
        restart=(restart.idle_threshold_seconds, restart.warmup_seconds) if restart else None,
        now=index.now,
    )


def _unplaceable(op_id: str, missed_max_lag: bool) -> ScheduleIssue:
    if missed_max_lag:
        message = f"{op_id}: no slot after its machine predecessor meets its max_lag"
        return _issue("max_lag_unmet", op_id, message)
    message = f"{op_id}: no slot after its machine predecessor within calendars, pins, operators"
    return _issue("no_feasible_slot", op_id, message)
