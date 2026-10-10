"""Elementary dispatch rules on the non-delay engine (COMP-009, REQ-090; Pinedo Table 14.1).

Each rule is a small class implementing `kernel.dispatch.PriorityRule` (higher index goes
first, ties by op id) and registers as a `DispatchSolver` (family `dispatch`,
deterministic, every `Feature`). Readings, for op o of job j on machine i at time t, with
`R_after(o)` the job's later ops' shortest runs (`remaining_shortest_run`):

- `fifo`: earliest material release max(job release, op ready_time), then earliest ready
  time (material gate from predecessors);
- `edd`: earliest job due (no due last);
- `wspt`: largest w_j / p_i(o);
- `spt` / `lpt`: shortest / longest p_i(o);
- `ms`: smallest slack d_j - t - (p_i(o) + R_after(o)) (no due last);
- `cr`: smallest (d_j - t) / (p_i(o) + R_after(o)), negative once overdue (no due last);
- `sst`: smallest setup from the machine's last placed op, or its state when none;
- `lfj`: fewest eligible machines, then EDD;
- `sqno`: shortest queue (ready, unplaced ops) at the next op's work center; a job's last
  op has queue 0.

A leaf of `algorithms/` (D-069): it registers through `SOLVERS` only.
"""

from __future__ import annotations

import functools
import math
from collections.abc import Callable, Mapping

from twinflow.scheduling.kernel.dispatch import (
    DispatchSolver,
    DispatchState,
    Priority,
    PriorityRule,
)
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature


def _due(state: DispatchState, op_id: str) -> float:
    """The op's job due, inf without one."""
    index = state.index
    due = index.jobs[index.op(op_id).job_pos].due
    return math.inf if due is None else due


def _remaining_work(state: DispatchState, op_id: str, machine_id: str) -> float:
    """p_i(o) + R_after(o): the job's processing left, the op itself included."""
    return state.option(op_id, machine_id).run + state.index.remaining_shortest_run(op_id)


class Fifo:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return (-state.index.release(op_id), -state.ready_time(op_id))


class Edd:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return -_due(state, op_id)


class Wspt:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        weight = state.index.jobs[state.index.op(op_id).job_pos].weight
        run = state.option(op_id, machine_id).run
        return math.inf if run == 0 else weight / run


class Spt:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return -state.option(op_id, machine_id).run


class Lpt:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return state.option(op_id, machine_id).run


class MinimumSlack:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        slack = _due(state, op_id) - t - _remaining_work(state, op_id, machine_id)
        return -slack


class CriticalRatio:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        due = _due(state, op_id)
        if due == math.inf:
            return -math.inf
        left, work = due - t, _remaining_work(state, op_id, machine_id)
        if work > 0:
            return -(left / work)
        return math.inf if left < 0 else -math.inf


class ShortestSetup:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return -state.index.setups.seconds(machine_id, last_op_id, op_id)


class LeastFlexibleJob:
    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        return (-float(len(state.index.op(op_id).options)), -_due(state, op_id))


class ShortestQueueNext:
    """Needs each op's next work center in its job: built once in `prepare`."""

    def __init__(self) -> None:
        self._next_center: dict[str, str | None] = {}

    def prepare(self, index: ProblemIndex) -> None:
        for job in index.jobs:
            centers = [index.ops[p].work_center_id for p in job.ops]
            nexts: list[str | None] = [*centers[1:], None]
            for p, center in zip(job.ops, nexts, strict=True):
                self._next_center[index.op_ids[p]] = center

    def index(
        self, state: DispatchState, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> Priority:
        center = self._next_center[op_id]
        return 0.0 if center is None else -float(state.queue_length(center))


RULES: Mapping[str, Callable[[], PriorityRule]] = {
    "fifo": Fifo,
    "edd": Edd,
    "wspt": Wspt,
    "spt": Spt,
    "lpt": Lpt,
    "ms": MinimumSlack,
    "cr": CriticalRatio,
    "sst": ShortestSetup,
    "lfj": LeastFlexibleJob,
    "sqno": ShortestQueueNext,
}

SOLVERS = tuple(
    SolverSpec(
        name=name,
        family="dispatch",
        factory=functools.partial(DispatchSolver, name, rule),
        deterministic=True,
        capabilities=frozenset(Feature),
    )
    for name, rule in RULES.items()
)
"""Registry entries (D-069)."""
