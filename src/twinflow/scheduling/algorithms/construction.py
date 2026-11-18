"""Construction: Giffler-Thompson, forward and backward loading (COMP-012, REQ-097, REQ-098).

Registers `gt_active`, `forward_load` and `backward_load` (family `construction`,
deterministic, every `Feature`). Param `rule` (`SolveContext.params`, default `atcs`): a
priority rule by registry name (`registry.rule`: every `dispatch`-family rule, `atc`,
`atcs`); any other value or param gives UNKNOWN with `invalid_method_param`. A missing
`atcs` leaf behind the default gives UNKNOWN with `subsolver_unavailable` (D-069).

- `gt_active` (Pinedo Alg 7.1.3, p.183-184): the dispatch engine in active mode with the
  rule. With setups, "active" means no op can start earlier without any other op's
  assignment changing (GT compares completions with the setup from the machine's last op).
- `forward_load` (p.473): ops in rule priority order from `now`, each at its earliest
  feasible start on its machine, idle gaps included (an op may land before ops of higher
  priority). Loading runs through a window [mine]: a ready op (every predecessor placed) is
  eligible while its estimated start max(machine free time, est) on its best accepted machine
  lies before the window start plus `DEFAULT_WINDOW` x the mean shortest run; eligible ops go
  in rule priority order (the highest index over the accepted machines, each read at the op's
  estimated start there; ties: op id), each on the accepted machine where it ends first with
  gap insertion (ties: machine id). An op whose estimated start left the window meanwhile
  waits; when none is eligible the window moves to the earliest waiting op. The window
  (0.75, tuned on the training seeds: 0.7 to 1.5 all pass there, smaller is better) is
  wide enough for the pinned case where priority outweighs a 3 s later start (pbar 4.5).
  Without the window (one priority order over every op) loading goes job by job and the idle
  gaps it leaves cannot be refilled under sequence-dependent setups (gap insertion keeps
  every placed follower's setup): about 3x EDD's score on the training bench. Placement goes
  through the engine's timelines; the per-machine sequences in start order then go through
  `decode` (DEV-10: append-only). The decode reproduces the loading or improves on it: in a
  topological order every op's machine predecessor and material gate are no later than in
  the loading, so its earliest slot after them is no later either (the loaded slot stays
  available); a regular objective cannot get worse.
- `backward_load` (p.474): a backward pass on the instance mirrored in time from H = the
  latest due (a job's release becomes H - due, 0 without a due, its due H - max(now,
  release); every arc reversed with its min lag; each setup family matrix transposed;
  calendars, pin windows, operators, batches, restart and machine state left out, since the
  mirror only plans). The non-delay engine with the rule schedules the mirror; an op's mirror
  start s gives its latest finish H - s (a job's last op: never after its due). The forward
  pass is the non-delay engine with the rule on the real problem where each due op's local
  due is its latest finish and each due job's due is its last op's [mine: decoding the
  reversed mirror sequences directly scored 2.8 to 3.6x EDD on the training bench, since the
  mirror's packing becomes forward idle that a left shift cannot remove].

A leaf of `algorithms/` (D-069): it registers through `SOLVERS` and composes rules by name.
"""

from __future__ import annotations

import dataclasses
import functools
import heapq
import math
import time
from collections.abc import Callable, Mapping, Sequence

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.decode import decode
from twinflow.scheduling.kernel.dispatch import (
    Dispatched,
    DispatchEngine,
    DispatchState,
    PriorityRule,
    dispatched_schedule,
)
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import (
    FamilyMatrixEntry,
    Job,
    Machine,
    MachineState,
    ObjectiveSpec,
    Operation,
    Precedence,
    Problem,
    ProcessingOption,
    Schedule,
    ScheduleIssue,
)
from twinflow.scheduling.registry import SolverSpec
from twinflow.scheduling.seam import Feature, ParamValue, SolveContext

DEFAULT_RULE = "atcs"
DEFAULT_WINDOW = 0.75
_DEADLINE_STRIDE = 64
"""Placements between deadline checks in forward loading."""

Build = Callable[[Problem, ProblemIndex, PriorityRule, float], Dispatched | ScheduleIssue]
"""(problem, index, rule, deadline) -> the method's plan or its issue."""


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path, message=message)


def _time_limit() -> ScheduleIssue:
    return _issue("time_limit", "budget_seconds", "construction ran out of time")


# --- rule from params ----------------------------------------------------------------------------


def rule_from_params(name: str, params: Mapping[str, ParamValue]) -> PriorityRule | ScheduleIssue:
    """The `rule` param as a fresh rule, else `invalid_method_param` / `subsolver_unavailable`."""
    path = f"profile.method_params.{name}"
    unknown = sorted(set(params) - {"rule"})
    if unknown:
        return _issue("invalid_method_param", path, f"unknown param {unknown[0]}")
    chosen = params.get("rule", DEFAULT_RULE)
    if not isinstance(chosen, str):
        return _issue("invalid_method_param", f"{path}.rule", "rule must be a rule name")
    try:
        return registry.rule(chosen)
    except registry.UnknownRuleError as error:
        if "rule" in params:
            return _issue("invalid_method_param", f"{path}.rule", str(error))
        return _issue("subsolver_unavailable", f"{path}.rule", str(error))


# --- gt_active -----------------------------------------------------------------------------------


def gt_active(
    problem: Problem, index: ProblemIndex, rule: PriorityRule, deadline: float
) -> Dispatched | ScheduleIssue:
    """Giffler-Thompson: the engine in active mode with `rule`."""
    return DispatchEngine(index, rule, mode="active").run(deadline)


# --- forward loading -----------------------------------------------------------------------------


def forward_load(
    problem: Problem, index: ProblemIndex, rule: PriorityRule, deadline: float
) -> Dispatched | ScheduleIssue:
    """Ops in rule priority order, each at its earliest start with gap insertion, decoded."""
    state = DispatchEngine(index, rule).initial_state()
    if state.issue is not None:
        return state.issue
    queue = _ForwardQueue(state, rule, _window_width(index))
    placed = 0
    while queue:
        p = queue.pop()
        issue = state.place(p, _earliest_finish_machine(state, p), gap_insertion=True)
        if issue is not None:
            return issue
        queue.push_ready(index.successors[p])
        placed += 1
        if placed % _DEADLINE_STRIDE == 0 and time.monotonic() > deadline:
            return _time_limit()
    choice, sequences = state.plan_by_start()
    decoded = decode(index, choice, sequences)
    if isinstance(decoded, ScheduleIssue):
        return decoded
    return Dispatched(choice=choice, sequences=sequences, decoded=decoded)


def _earliest_finish_machine(state: DispatchState, p: int) -> int:
    """The accepted machine where `p` ends first with gap insertion (ties: machine id).

    When no accepted machine has a slot, the first one: placing there names the issue.
    """
    names = state.index.machines
    accepted = sorted(state.accepted(p), key=lambda m: names[m].id)
    trials = [(state.trial_end(p, m, gap_insertion=True), m) for m in accepted]
    ends = [(end, m) for end, m in trials if end is not None]
    if not ends:
        return accepted[0]
    return min(ends, key=lambda pair: (pair[0], names[pair[1]].id))[1]


class _ForwardQueue:
    """Ready ops in rule priority order within a loading window (module docstring).

    `pending` holds ready ops by estimated start; those with an estimated start before
    `window_start + width` are `eligible`, keyed by their rule priority (highest first; ties:
    op id). When no op is eligible the window moves to the earliest pending op.
    """

    __slots__ = ("_eligible", "_pending", "_queued", "_rule", "_state", "_width", "_window_start")

    def __init__(self, state: DispatchState, rule: PriorityRule, width: float) -> None:
        self._state = state
        self._rule = rule
        self._width = width
        self._window_start = state.index.now
        self._eligible: list[tuple[tuple[float, ...], str, int]] = []
        self._pending: list[tuple[float, str, int]] = []
        self._queued: set[int] = set()
        machines = range(len(state.index.machines))
        self.push_ready(sorted({p for m in machines for p in state.candidates(m)}))

    def __bool__(self) -> bool:
        return bool(self._eligible or self._pending)

    def pop(self) -> int:
        while True:
            if not self._eligible:
                self._window_start = self._pending[0][0]
                self._admit()
                continue
            _, op_id, p = heapq.heappop(self._eligible)
            fresh = self._estimated_start(p)
            if fresh < self._window_start + self._width:
                return p
            heapq.heappush(self._pending, (fresh, op_id, p))  # its machine filled up meanwhile

    def push_ready(self, positions: Sequence[int]) -> None:
        """Queue each op of `positions` that is ready now and not queued yet."""
        index = self._state.index
        for p in positions:
            if p not in self._queued and self._is_ready(p):
                self._queued.add(p)
                heapq.heappush(self._pending, (self._estimated_start(p), index.op_ids[p], p))
        self._admit()

    def _admit(self) -> None:
        """Move pending ops whose estimated start falls in the window to `eligible`."""
        limit = self._window_start + self._width
        while self._pending and self._pending[0][0] < limit:
            stale, op_id, p = heapq.heappop(self._pending)
            fresh = self._estimated_start(p)
            if fresh > stale:  # its machines filled up since: estimated starts only grow
                heapq.heappush(self._pending, (fresh, op_id, p))
                continue
            key = tuple(-v for v in self._priority(p))
            heapq.heappush(self._eligible, (key, op_id, p))

    def _is_ready(self, p: int) -> bool:
        index = self._state.index
        m = index.machine_pos[index.ops[p].options[0].machine_id]
        return p in self._state.candidates(m)

    def _estimated_start(self, p: int) -> float:
        """max(machine free time, est) on the op's best accepted machine."""
        return min(self._state.estimated_start(p, m) for m in self._state.accepted(p))

    def _priority(self, p: int) -> tuple[float, ...]:
        """The highest rule index over the accepted machines, each at the op's start there."""
        state, index = self._state, self._state.index
        op_id = index.op_ids[p]
        keys = []
        for m in state.accepted(p):
            machine_id = index.machines[m].id
            t = state.estimated_start(p, m)
            value = self._rule.index(state, op_id, machine_id, t, state.last_op(machine_id))
            keys.append(value if isinstance(value, tuple) else (value,))
        return max(keys)


def _window_width(index: ProblemIndex) -> float:
    """`DEFAULT_WINDOW` times the mean shortest run of the not-running ops."""
    runs = [min(o.run for o in op.options) for op in index.ops if op.in_progress is None]
    mean = sum(runs) / len(runs) if runs else 0.0
    return DEFAULT_WINDOW * mean if mean > 0 else DEFAULT_WINDOW


# --- backward loading ----------------------------------------------------------------------------


def backward_load(
    problem: Problem, index: ProblemIndex, rule: PriorityRule, deadline: float
) -> Dispatched | ScheduleIssue:
    """Plan latest finishes on the time mirror, then dispatch forward to them (docstring)."""
    horizon = max((job.due for job in index.jobs if job.due is not None), default=index.now)
    mirror = ProblemIndex.build(_mirror(problem, index, horizon), index.phi)
    mirrored = DispatchEngine(mirror, rule).run(deadline)
    if isinstance(mirrored, ScheduleIssue):
        return mirrored
    latest = {op_id: horizon - mirrored.decoded.start(op_id) for op_id in mirrored.choice}
    planned = DispatchEngine(_with_latest_finishes(index, latest), rule).run(deadline)
    if isinstance(planned, ScheduleIssue):
        return planned
    decoded = dataclasses.replace(planned.decoded, index=index)
    return Dispatched(choice=planned.choice, sequences=planned.sequences, decoded=decoded)


def _with_latest_finishes(index: ProblemIndex, latest: Mapping[str, float]) -> ProblemIndex:
    """`index` with each due op's local due set to its latest finish, each due job's due to
    min(due, its last op's latest finish) (the mirror releases no op before H - due, so that
    is the due unless capacity pulls it earlier). Ops of jobs without a due, and in-progress
    ops (not mirrored), keep their dates.
    """
    ops = tuple(
        dataclasses.replace(op, local_due=latest[op.id])
        if op.local_due is not None and op.id in latest
        else op
        for op in index.ops
    )
    jobs = tuple(
        dataclasses.replace(job, due=min(job.due, _job_latest(job.ops, index, latest)))
        if job.due is not None
        else job
        for job in index.jobs
    )
    return dataclasses.replace(index, ops=ops, jobs=jobs)


def _job_latest(
    positions: tuple[int, ...], index: ProblemIndex, latest: Mapping[str, float]
) -> float:
    """The latest finish of the job's last op (inf when it runs now: it is not mirrored)."""
    return latest.get(index.op_ids[positions[-1]], math.inf)


def _mirror(problem: Problem, index: ProblemIndex, horizon: float) -> Problem:
    """The scheduled, not-running ops mirrored in time from `horizon`; `now` is 0."""
    mirrored = {op.id for op in index.ops if op.in_progress is None}
    reversed_links = _reversed_links(index, mirrored)
    jobs = []
    for job in problem.jobs:
        job_pos = index.job_pos.get(job.id)
        operations = tuple(
            _mirror_op(op, index, reversed_links.get(op.id, []))
            for op in reversed(job.operations)
            if op.id in mirrored
        )
        if job_pos is None or not operations:
            continue
        jobs.append(_mirror_job(job, index, job_pos, operations, horizon))
    return problem.model_copy(
        update={
            "now": 0.0,
            "horizon_end": None,
            "jobs": tuple(jobs),
            "machines": tuple(_mirror_machine(m) for m in problem.machines),
            "calendars": (),
            "operator_pools": (),
            "setup_functions": tuple(
                f.model_copy(update={"family_matrix": _transposed(f.family_matrix)})
                for f in problem.setup_functions
            ),
        }
    )


def _reversed_links(index: ProblemIndex, mirrored: set[str]) -> dict[str, list[Precedence]]:
    """Per mirrored op, its mirror predecessors: its forward successors, min lags kept."""
    links: dict[str, list[Precedence]] = {}
    for op_data in index.ops:
        for link in op_data.predecessors:
            if op_data.id in mirrored and link.pred_id in mirrored:
                precedence = Precedence(operation_id=op_data.id, min_lag=link.min_lag)
                links.setdefault(link.pred_id, []).append(precedence)
    return links


def _mirror_op(op: Operation, index: ProblemIndex, predecessors: list[Precedence]) -> Operation:
    options = tuple(
        ProcessingOption(
            machine_id=o.machine_id,
            fixed_seconds=o.fixed_seconds,
            unit_seconds=o.unit_seconds,
            preference_cost=o.preference_cost,
        )
        for o in index.op(op.id).options
    )
    return Operation(
        id=op.id,
        work_center_id=op.work_center_id,
        quantity=op.quantity,
        options=options,
        predecessors=tuple(predecessors),
        setup_params=op.setup_params,
        setup_family=op.setup_family,
    )


def _mirror_job(
    job: Job,
    index: ProblemIndex,
    job_pos: int,
    operations: tuple[Operation, ...],
    horizon: float,
) -> Job:
    """Release H - due (0 without a due), due H - max(now, release), the effective weight."""
    due = index.jobs[job_pos].due
    return Job(
        id=job.id,
        weight_override=index.jobs[job_pos].weight,
        release=0.0 if due is None else horizon - due,
        due=horizon - max(index.now, job.release),
        operations=operations,
    )


def _mirror_machine(machine: Machine) -> Machine:
    update = {"calendar_id": None, "batch": None, "restart": None, "state": MachineState()}
    return machine.model_copy(update=update)


def _transposed(matrix: tuple[FamilyMatrixEntry, ...]) -> tuple[FamilyMatrixEntry, ...]:
    return tuple(
        e.model_copy(update={"from_family": e.to_family, "to_family": e.from_family})
        for e in matrix
    )


# --- the solver ----------------------------------------------------------------------------------


class ConstructionSolver:
    """One construction method behind the `Solver` seam (COMP-012).

    Returns FEASIBLE with the primary objective, or UNKNOWN with the method's issue.
    """

    __slots__ = ("_build", "_name")

    def __init__(self, name: str, build: Build) -> None:
        self._name = name
        self._build = build

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> frozenset[Feature]:
        """Every feature: placement goes through the shared timelines and the decoder."""
        return frozenset(Feature)

    def solve(
        self,
        problem: Problem,
        *,
        objective: ObjectiveSpec,
        budget_seconds: float,
        context: SolveContext,
    ) -> Schedule:
        """One construction pass, cut off with `time_limit` at `context.deadline`."""
        started = time.perf_counter()
        rule = rule_from_params(self._name, context.params)
        index = ProblemIndex.build(problem)
        if isinstance(rule, ScheduleIssue):
            result: Dispatched | ScheduleIssue = rule
        else:
            result = self._build(problem, index, rule, context.deadline)
        return dispatched_schedule(self._name, result, index, objective, context, started)


_METHODS: Mapping[str, Build] = {
    "gt_active": gt_active,
    "forward_load": forward_load,
    "backward_load": backward_load,
}

SOLVERS = tuple(
    SolverSpec(
        name=name,
        family="construction",
        factory=functools.partial(ConstructionSolver, name, build),
        deterministic=True,
        capabilities=frozenset(Feature),
    )
    for name, build in _METHODS.items()
)
"""Registry entries (D-069)."""
