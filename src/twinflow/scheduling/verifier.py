"""Independent schedule verifier (COMP-007, REQ-073, REQ-074).

`verify` re-derives every hard constraint of a compiled problem from the SDD "Data Model"
formulas and checks a schedule against them. It shares no code with any solver: it
imports only `model`, `setups`, `objectives` and `calendar_math` (boundary test), so a
solver bug cannot hide behind the code that checks it.

Every issue has a code from COMP-007, a path (the operation id, the batch id, the override
id or `primary_value`) and a message. All time comparisons allow an absolute tolerance of
`TOLERANCE` seconds. Work is O(n log n) in the number of assignments: lookups go through
an index built once, machine sequences and operator usage are sorted sweeps.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from twinflow.scheduling import calendar_math
from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.model import (
    MAX_TEXT_LENGTH,
    Assignment,
    CompiledProblem,
    ExcludeMachineOverride,
    HoldOverride,
    Interval,
    Job,
    LockOverride,
    Machine,
    Operation,
    OperatorNeed,
    OperatorPool,
    Override,
    Precedence,
    PreferMachineOverride,
    ProcessingOption,
    Schedule,
    ScheduleIssue,
    VerificationResult,
)
from twinflow.scheduling.objectives import evaluate, primary_kind
from twinflow.scheduling.setups import SetupTable

TOLERANCE = 1e-6
"""Absolute slack in seconds for every time comparison; also abs/rel slack of the objective."""

PRIMARY_PATH = "primary_value"


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path[:MAX_TEXT_LENGTH], message=message[:MAX_TEXT_LENGTH])


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE


# --- index ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Index:
    """Lookups over one compiled problem, built once per `verify` call."""

    compiled: CompiledProblem
    job: Mapping[str, Job]
    job_of: Mapping[str, Job]
    operation: Mapping[str, Operation]
    option: Mapping[tuple[str, str], ProcessingOption]
    machine: Mapping[str, Machine]
    calendar: Mapping[str, tuple[Interval, ...]]
    pool: Mapping[str, OperatorPool]
    held_jobs: frozenset[str]
    setups: SetupTable
    _effective: dict[tuple[str | None, ...], Intervals] = field(default_factory=dict)

    def calendar_of(self, calendar_id: str | None) -> Intervals:
        """Working intervals of a calendar id; None means always working."""
        return None if calendar_id is None else self.calendar[calendar_id]

    def effective_calendar(self, machine: Machine, op: Operation) -> Intervals:
        """Machine calendar intersected with every needed pool's calendar (memoized)."""
        ids = (machine.calendar_id, *(self.pool[n.pool_id].calendar_id for n in op.operator_needs))
        if ids not in self._effective:
            intervals = self.calendar_of(ids[0])
            for calendar_id in ids[1:]:
                intervals = calendar_math.intersect(intervals, self.calendar_of(calendar_id))
            self._effective[ids] = intervals
        return self._effective[ids]


def _build_index(compiled: CompiledProblem, raw_overrides: Sequence[Override]) -> _Index:
    """Index the problem. Held jobs: compiled holds, input status `held`, raw holds w/o until."""
    problem = compiled.problem
    job_of = {op.id: job for job in problem.jobs for op in job.operations}
    operation = {op.id: op for job in problem.jobs for op in job.operations}
    option = {(op.id, o.machine_id): o for op in operation.values() for o in op.options}
    raw_holds = {o.job_id for o in raw_overrides if isinstance(o, HoldOverride) and o.until is None}
    held = {job.id for job in problem.jobs if job.status == "held"}
    return _Index(
        compiled=compiled,
        job={job.id: job for job in problem.jobs},
        job_of=job_of,
        operation=operation,
        option=option,
        machine={m.id: m for m in problem.machines},
        calendar={c.id: c.intervals for c in problem.calendars},
        pool={p.id: p for p in problem.operator_pools},
        held_jobs=frozenset(held | raw_holds | set(compiled.held_job_ids)),
        setups=SetupTable.for_problem(problem),
    )


# --- public API ---------------------------------------------------------------------------------


def verify(
    compiled: CompiledProblem, raw_overrides: Sequence[Override], schedule: Schedule
) -> VerificationResult:
    """Check `schedule` against every hard constraint of `compiled` (REQ-074).

    `raw_overrides` is the request's override list: hard overrides are checked against it
    directly, so an override the compile step lost is still enforced (D-066). The reported
    `primary_value` is compared with `objectives.evaluate` for the primary kind of
    `compiled.profile.objective`, only when every other check passed (the objective of an
    infeasible schedule has no meaning). Non-finite times stop verification early.
    """
    nonfinite = list(_nonfinite_issues(schedule))
    if nonfinite:
        return VerificationResult(valid=False, issues=tuple(nonfinite))
    index = _build_index(compiled, raw_overrides)
    scoped, issues = _scope(index, schedule.assignments)
    by_op = {a.operation_id: a for a in scoped}
    sequences = _machine_sequences(index, scoped)
    issues.extend(_missing_issues(index, by_op))
    issues.extend(_assignment_issues(index, scoped, by_op, _setup_predecessors(sequences)))
    issues.extend(_machine_overlap_issues(sequences))
    issues.extend(_operator_capacity_issues(index, scoped))
    issues.extend(_batch_issues(index, scoped))
    issues.extend(_override_issues(index, by_op, raw_overrides))
    if not issues:
        issues.extend(_objective_issues(compiled, schedule))
    return VerificationResult(valid=not issues, issues=tuple(issues))


# --- finiteness and coverage --------------------------------------------------------------------


def _nonfinite_issues(schedule: Schedule) -> Iterator[ScheduleIssue]:
    """`nonfinite`: every time and the reported primary value must be finite."""
    for a in schedule.assignments:
        if not all(math.isfinite(t) for t in (a.start, a.setup_end, a.end)):
            yield _issue("nonfinite", a.operation_id, f"{a.operation_id}: non-finite time")
    value = schedule.primary_value
    if value is not None and not math.isfinite(value):
        yield _issue("nonfinite", PRIMARY_PATH, f"primary_value {value} is not finite")


def _scope(
    index: _Index, assignments: Iterable[Assignment]
) -> tuple[list[Assignment], list[ScheduleIssue]]:
    """Assignments the remaining checks read, plus coverage issues for the rest.

    `unknown_assignment`: the operation is not in the compiled problem, or it was already
    assigned (an operation runs once). `held_scheduled`: the operation's job is held.
    """
    scoped: list[Assignment] = []
    seen: set[str] = set()
    issues: list[ScheduleIssue] = []
    for a in assignments:
        op_id = a.operation_id
        if op_id not in index.operation:
            issues.append(_issue("unknown_assignment", op_id, f"{op_id}: unknown operation"))
        elif op_id in seen:
            issues.append(_issue("unknown_assignment", op_id, f"{op_id}: assigned twice"))
        elif index.job_of[op_id].id in index.held_jobs:
            issues.append(_issue("held_scheduled", op_id, f"{op_id}: its job is held"))
        else:
            scoped.append(a)
            seen.add(op_id)
    return scoped, issues


def _missing_issues(index: _Index, by_op: Mapping[str, Assignment]) -> Iterator[ScheduleIssue]:
    """`missing_assignment`: every operation not done, of a job neither done nor held."""
    for op_id, op in index.operation.items():
        job = index.job_of[op_id]
        if job.id in index.held_jobs or job.status == "done" or op.status.state == "done":
            continue
        if op_id not in by_op:
            yield _issue("missing_assignment", op_id, f"{op_id}: no assignment")


# --- machine sequences --------------------------------------------------------------------------


def _machine_sequences(index: _Index, scoped: Iterable[Assignment]) -> dict[str, list[Assignment]]:
    """Assignments on known machines, per machine, sorted by (start, end, operation id).

    `end` breaks start ties so an empty span [t, t) comes before an operation starting at
    t: the order, and with it every overlap and setup verdict, never depends on ids.
    """
    sequences: dict[str, list[Assignment]] = defaultdict(list)
    for a in scoped:
        if a.machine_id in index.machine:
            sequences[a.machine_id].append(a)
    for sequence in sequences.values():
        sequence.sort(key=lambda a: (a.start, a.end, a.operation_id))
    return sequences


def _setup_predecessors(sequences: Mapping[str, Sequence[Assignment]]) -> dict[str, str | None]:
    """The operation each setup is measured from: the previous one in sequence order.

    None means the machine's state at `now`. Members of one batch share the predecessor of
    the batch (the setup is done once for the whole load).
    """
    predecessor: dict[str, str | None] = {}
    for sequence in sequences.values():
        last: Assignment | None = None
        for a in sequence:
            same_batch = last is not None and a.batch_id is not None and a.batch_id == last.batch_id
            if last is None:
                predecessor[a.operation_id] = None
            elif same_batch:
                predecessor[a.operation_id] = predecessor[last.operation_id]
            else:
                predecessor[a.operation_id] = last.operation_id
            last = a
    return predecessor


def _machine_overlap_issues(
    sequences: Mapping[str, Sequence[Assignment]],
) -> Iterator[ScheduleIssue]:
    """`machine_overlap`: a unary machine is busy on [start, end); members of one batch share it.

    Each assignment is compared with the latest-ending assignment before it, so an
    assignment nested inside a long one is caught as well as a neighbor. An empty span
    [t, t) overlaps an operation on [s, e) only when s < t < e.
    """
    for sequence in sequences.values():
        frontier: Assignment | None = None
        for a in sequence:
            if frontier is not None and a.start < frontier.end - TOLERANCE:
                if a.batch_id is None or a.batch_id != frontier.batch_id:
                    message = f"{a.operation_id} starts before {frontier.operation_id} ends"
                    yield _issue("machine_overlap", a.operation_id, message)
            if frontier is None or a.end > frontier.end:
                frontier = a


# --- per-assignment checks ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Placement:
    """One eligible, pending assignment with what its checks read."""

    assignment: Assignment
    operation: Operation
    job: Job
    machine: Machine
    option: ProcessingOption
    predecessor: str | None

    @property
    def run_seconds(self) -> float:
        """r = fixed + quantity * unit of the chosen option."""
        return self.option.fixed_seconds + self.operation.quantity * self.option.unit_seconds

    @property
    def gate(self) -> tuple[str, float]:
        """Material gate g: `start` with attached setup, `setup_end` with detached setup."""
        if self.operation.setup_detached:
            return "setup_end", self.assignment.setup_end
        return "start", self.assignment.start


def _assignment_issues(
    index: _Index,
    scoped: Iterable[Assignment],
    by_op: Mapping[str, Assignment],
    predecessors: Mapping[str, str | None],
) -> Iterator[ScheduleIssue]:
    """In-progress placement, eligibility, then every per-operation family."""
    for a in scoped:
        op = index.operation[a.operation_id]
        if op.status.state == "in_progress":
            yield from _in_progress_issues(index, op, a)
            continue
        option = index.option.get((op.id, a.machine_id))
        if option is None or a.machine_id not in index.machine:
            message = f"{op.id}: machine {a.machine_id} is not an option"
            yield _issue("ineligible_machine", op.id, message)
            continue
        placement = _Placement(
            a, op, index.job_of[op.id], index.machine[a.machine_id], option, predecessors[op.id]
        )
        yield from _length_issues(index, placement)
        yield from _readiness_issues(index, placement)
        yield from _pin_issues(placement)
        yield from _precedence_issues(index, by_op, placement)


def _in_progress_issues(index: _Index, op: Operation, a: Assignment) -> Iterator[ScheduleIssue]:
    """`in_progress`: on its status machine at `now`, setup 0, run = remaining seconds."""
    now = index.compiled.problem.now
    end = now + (op.status.remaining_seconds or 0.0)
    placed = (
        a.machine_id == op.status.machine_id
        and _close(a.start, now)
        and _close(a.setup_end, a.start)
        and _close(a.end, end)
    )
    if not placed:
        message = (
            f"{op.id}: in progress on {op.status.machine_id} must be [{now:g}, {end:g}), "
            f"got {a.machine_id} [{a.start:g}, {a.end:g})"
        )
        yield _issue("in_progress", op.id, message)


def _length_issues(index: _Index, p: _Placement) -> Iterator[ScheduleIssue]:
    """Setup and run lengths and calendar fit, by the operation's `shift_crossing` rule."""
    setup = index.setups.seconds(p.machine.id, p.predecessor, p.operation.id)
    if p.operation.shift_crossing == "pause":
        yield from _pause_issues(index, p, setup)
    else:
        yield from _forbid_issues(index, p, setup)


def _forbid_issues(index: _Index, p: _Placement, setup: float) -> Iterator[ScheduleIssue]:
    """`forbid`: setup_end - start = setup, end - setup_end = r, [start, end) in one interval.

    The machine calendar gives `calendar`; each needed pool's calendar gives
    `operator_calendar` (the effective calendar is their intersection).
    """
    a, op_id = p.assignment, p.operation.id
    if not _close(a.end - a.setup_end, p.run_seconds):
        message = f"{op_id}: run {a.end - a.setup_end:g} s, expected {p.run_seconds:g} s"
        yield _issue("duration", op_id, message)
    if not _close(a.setup_end - a.start, setup):
        message = f"{op_id}: setup {a.setup_end - a.start:g} s, expected {setup:g} s"
        yield _issue("setup", op_id, message)
    if not calendar_math.fits_within(index.calendar_of(p.machine.calendar_id), a.start, a.end):
        message = f"{op_id}: [{a.start:g}, {a.end:g}) leaves machine {p.machine.id}'s calendar"
        yield _issue("calendar", op_id, message)
    for need in p.operation.operator_needs:
        intervals = index.calendar_of(index.pool[need.pool_id].calendar_id)
        if not calendar_math.fits_within(intervals, a.start, a.end):
            message = f"{op_id}: [{a.start:g}, {a.end:g}) leaves pool {need.pool_id}'s calendar"
            yield _issue("operator_calendar", op_id, message)


def _pause_issues(index: _Index, p: _Placement, setup: float) -> Iterator[ScheduleIssue]:
    """`pause`: setup then run consume work seconds of the effective calendar only.

    work_seconds(start, setup_end) = setup, and end = finish_after_work(start, setup + r).
    """
    a, op_id = p.assignment, p.operation.id
    calendar = index.effective_calendar(p.machine, p.operation)
    setup_work = calendar_math.work_seconds(calendar, a.start, a.setup_end)
    if not _close(setup_work, setup):
        message = f"{op_id}: setup has {setup_work:g} working s, expected {setup:g} s"
        yield _issue("setup", op_id, message)
    finish = calendar_math.finish_after_work(calendar, a.start, setup + p.run_seconds)
    if finish is None or not _close(finish, a.end):
        expected = "never" if finish is None else f"{finish:g}"
        message = f"{op_id}: end {a.end:g}, paused work finishes at {expected}"
        yield _issue("calendar", op_id, message)


def _readiness_issues(index: _Index, p: _Placement) -> Iterator[ScheduleIssue]:
    """`readiness`: start >= max(now, available_from) and g >= max(release, ready_time)."""
    a, op_id = p.assignment, p.operation.id
    machine_ready = max(index.compiled.problem.now, p.machine.state.available_from)
    if a.start < machine_ready - TOLERANCE:
        message = f"{op_id}: start {a.start:g} is before max(now, available_from) {machine_ready:g}"
        yield _issue("readiness", op_id, message)
    gate_name, gate = p.gate
    material_ready = max(p.job.release, p.operation.ready_time)
    if gate < material_ready - TOLERANCE:
        message = (
            f"{op_id}: {gate_name} {gate:g} is before max(release, ready_time) {material_ready:g}"
        )
        yield _issue("readiness", op_id, message)


def _pin_issues(p: _Placement) -> Iterator[ScheduleIssue]:
    """`pin`: pinned machine, and start within [earliest_start, latest_start]."""
    pin, a = p.operation.pin, p.assignment
    if pin is None:
        return
    wrong_machine = pin.machine_id is not None and pin.machine_id != a.machine_id
    too_early = pin.earliest_start is not None and a.start < pin.earliest_start - TOLERANCE
    too_late = pin.latest_start is not None and a.start > pin.latest_start + TOLERANCE
    if wrong_machine or too_early or too_late:
        message = f"{p.operation.id}: {a.machine_id} at {a.start:g} breaks its pin {pin}"
        yield _issue("pin", p.operation.id, message)


# --- precedence ---------------------------------------------------------------------------------


def _precedence_issues(
    index: _Index, by_op: Mapping[str, Assignment], p: _Placement
) -> Iterator[ScheduleIssue]:
    """Lags from each predecessor's end (or transfer lot) to this operation's material gate.

    A done predecessor counts as finished; an unassigned one is reported as missing.
    """
    for link in p.operation.predecessors:
        if index.operation[link.operation_id].status.state == "done":
            continue
        pred = by_op.get(link.operation_id)
        if pred is None:
            continue
        if link.overlap_quantity is None:
            yield from _end_to_gate_issues(link, pred, p)
        else:
            yield from _transfer_lot_issues(index, link, link.overlap_quantity, pred, p)


def _end_to_gate_issues(
    link: Precedence, pred: Assignment, p: _Placement
) -> Iterator[ScheduleIssue]:
    """`precedence`: g >= pred.end + min_lag; `max_lag`: g <= pred.end + max_lag."""
    gate_name, gate = p.gate
    op_id = p.operation.id
    earliest = pred.end + link.min_lag
    if gate < earliest - TOLERANCE:
        message = (
            f"{op_id}: {gate_name} {gate:g} is before predecessor {pred.operation_id} "
            f"end {pred.end:g} + min_lag {link.min_lag:g}"
        )
        yield _issue("precedence", op_id, message)
    if link.max_lag is not None and gate > pred.end + link.max_lag + TOLERANCE:
        message = (
            f"{op_id}: {gate_name} {gate:g} is after predecessor {pred.operation_id} "
            f"end {pred.end:g} + max_lag {link.max_lag:g}"
        )
        yield _issue("max_lag", op_id, message)


def _transfer_lot_issues(
    index: _Index, link: Precedence, quantity: float, pred: Assignment, p: _Placement
) -> Iterator[ScheduleIssue]:
    """Overlap with transfer lot q, with T = pred.setup_end + pred.fixed + q * pred.unit.

    `overlap_lag`: g >= T + min_lag and end >= pred.end + q * succ.unit (the last lot still
    needs its own run after the predecessor's last piece). `max_lag`: g <= T + max_lag.
    """
    pred_option = index.option.get((pred.operation_id, pred.machine_id))
    if pred_option is None:
        return  # the predecessor is reported as ineligible
    transfer = pred.setup_end + pred_option.fixed_seconds + quantity * pred_option.unit_seconds
    gate_name, gate = p.gate
    op_id, a = p.operation.id, p.assignment
    last_lot_end = pred.end + quantity * p.option.unit_seconds
    if gate < transfer + link.min_lag - TOLERANCE or a.end < last_lot_end - TOLERANCE:
        message = (
            f"{op_id}: needs {gate_name} >= {transfer + link.min_lag:g} and end >= "
            f"{last_lot_end:g} after transfer lot of {pred.operation_id}"
        )
        yield _issue("overlap_lag", op_id, message)
    if link.max_lag is not None and gate > transfer + link.max_lag + TOLERANCE:
        message = (
            f"{op_id}: {gate_name} {gate:g} is after transfer of {pred.operation_id} "
            f"at {transfer:g} + max_lag {link.max_lag:g}"
        )
        yield _issue("max_lag", op_id, message)


# --- operators ----------------------------------------------------------------------------------


def _need_span(need: OperatorNeed, a: Assignment) -> tuple[float, float]:
    """The phase a pool is held: setup [start, setup_end), run [setup_end, end), both."""
    if need.phase == "setup":
        return a.start, a.setup_end
    if need.phase == "run":
        return a.setup_end, a.end
    return a.start, a.end


def _operator_capacity_issues(
    index: _Index, scoped: Iterable[Assignment]
) -> Iterator[ScheduleIssue]:
    """`operator_capacity`: at every instant, a pool's usage is at most its size."""
    usage: dict[str, list[tuple[float, float, int, str]]] = defaultdict(list)
    for a in scoped:
        for need in index.operation[a.operation_id].operator_needs:
            start, end = _need_span(need, a)
            if end > start:
                usage[need.pool_id].append((start, end, need.quantity, a.operation_id))
    for pool_id in sorted(usage):
        yield from _pool_sweep(index.pool[pool_id], usage[pool_id])


def _pool_sweep(
    pool: OperatorPool, spans: Iterable[tuple[float, float, int, str]]
) -> Iterator[ScheduleIssue]:
    """Event sweep over half-open spans: at equal times releases come before acquisitions.

    Each operation whose acquisition takes the pool over its size is reported once.
    """
    events = sorted(
        event
        for start, end, quantity, op_id in spans
        for event in ((end, 0, -quantity, op_id), (start, 1, quantity, op_id))
    )
    level = 0
    reported: set[str] = set()
    for time, _, delta, op_id in events:
        level += delta
        if delta > 0 and level > pool.size and op_id not in reported:
            reported.add(op_id)
            message = f"{op_id}: pool {pool.id} needs {level} > size {pool.size} at {time:g}"
            yield _issue("operator_capacity", op_id, message)


# --- batches ------------------------------------------------------------------------------------


def _batch_issues(index: _Index, scoped: Iterable[Assignment]) -> Iterator[ScheduleIssue]:
    """`batch`: members of one (machine, batch_id) share start, setup_end, end and a set
    `batch_family`, and their total quantity fits the machine's batch capacity.
    """
    groups: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for a in scoped:
        if a.batch_id is not None:
            groups[a.machine_id, a.batch_id].append(a)
    for (machine_id, batch_id), members in sorted(groups.items()):
        problem = _batch_problem(index, machine_id, members)
        if problem is not None:
            member_ids = ", ".join(a.operation_id for a in members)
            yield _issue("batch", batch_id, f"batch {batch_id} ({member_ids}): {problem}")


def _batch_problem(index: _Index, machine_id: str, members: Sequence[Assignment]) -> str | None:
    """Why the batch is invalid, or None."""
    first = members[0]
    times = (first.start, first.setup_end, first.end)
    if any(not all(map(_close, times, (a.start, a.setup_end, a.end))) for a in members):
        return "members do not share start, setup_end and end"
    families = {index.operation[a.operation_id].batch_family for a in members}
    if None in families or len(families) != 1:
        return f"members mix batch families {sorted(map(str, families))}"
    machine = index.machine.get(machine_id)
    capacity = machine.batch.capacity if machine is not None and machine.batch else 0.0
    load = sum(index.operation[a.operation_id].quantity for a in members)
    if load > capacity + TOLERANCE:
        return f"load {load:g} exceeds capacity {capacity:g}"
    return None


# --- hard overrides -----------------------------------------------------------------------------


def _override_issues(
    index: _Index, by_op: Mapping[str, Assignment], raw_overrides: Iterable[Override]
) -> Iterator[ScheduleIssue]:
    """`override`: every hard lock, machine override and hold-until is honored.

    Targets resolve through `split_map`, so an override on a split job or operation applies
    to its children. A hold without `until` is enforced as `held_scheduled` in coverage.
    """
    for override in raw_overrides:
        if override.hardness == "hard" and not _honored(index, by_op, override):
            message = f"override {override.id} ({override.kind}) is not honored"
            yield _issue("override", override.id, message)


def _honored(index: _Index, by_op: Mapping[str, Assignment], override: Override) -> bool:
    if isinstance(override, LockOverride):
        targets = _resolve(index, override.operation_id)
        return all(_lock_holds(override, by_op.get(op_id)) for op_id in targets)
    if isinstance(override, ExcludeMachineOverride | PreferMachineOverride):
        assigned = [by_op[op_id] for op_id in _machine_targets(index, override) if op_id in by_op]
        excluded = isinstance(override, ExcludeMachineOverride)
        return all((a.machine_id == override.machine_id) != excluded for a in assigned)
    if isinstance(override, HoldOverride) and override.until is not None:
        return _hold_until_holds(index, by_op, override.job_id, override.until)
    return True


def _resolve(index: _Index, target_id: str) -> tuple[str, ...]:
    """A job or operation id as it is in the compiled problem (split children, else itself)."""
    return index.compiled.split_map.get(target_id, (target_id,))


def _ops_of_jobs(index: _Index, job_ids: Iterable[str]) -> list[str]:
    """Operation ids of the jobs that are in the compiled problem (held jobs are gone)."""
    jobs = (index.job[job_id] for job_id in job_ids if job_id in index.job)
    return [op.id for job in jobs for op in job.operations]


def _machine_targets(
    index: _Index, override: ExcludeMachineOverride | PreferMachineOverride
) -> Sequence[str]:
    if override.operation_id is not None:
        return _resolve(index, override.operation_id)
    if override.job_id is not None:
        return _ops_of_jobs(index, _resolve(index, override.job_id))
    return ()


def _lock_holds(lock: LockOverride, a: Assignment | None) -> bool:
    """Locked machine and exact start; an unassigned operation is reported as missing."""
    if a is None:
        return True
    machine_ok = lock.machine_id is None or a.machine_id == lock.machine_id
    start_ok = lock.start is None or _close(a.start, lock.start)
    return machine_ok and start_ok


def _hold_until_holds(
    index: _Index, by_op: Mapping[str, Assignment], job_id: str, until: float
) -> bool:
    """No operation of the job passes its material gate before `until` (release semantics)."""
    for op_id in _ops_of_jobs(index, _resolve(index, job_id)):
        a = by_op.get(op_id)
        if a is None:
            continue
        gate = a.setup_end if index.operation[op_id].setup_detached else a.start
        if gate < until - TOLERANCE:
            return False
    return True


# --- objective ----------------------------------------------------------------------------------


def _objective_issues(compiled: CompiledProblem, schedule: Schedule) -> Iterator[ScheduleIssue]:
    """`objective_mismatch`: the reported primary value equals the evaluated one (1e-6 abs/rel)."""
    reported = schedule.primary_value
    if reported is None:
        return
    kind = primary_kind(compiled.profile.objective)
    actual = evaluate(compiled, schedule)[kind]
    if not math.isclose(actual, reported, rel_tol=TOLERANCE, abs_tol=TOLERANCE):
        message = f"reported {kind} {reported:g}, evaluated {actual:g}"
        yield _issue("objective_mismatch", PRIMARY_PATH, message)
