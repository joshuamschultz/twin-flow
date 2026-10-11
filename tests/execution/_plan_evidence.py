"""Which features a generated plan actually exercises (T-172 anti-vacuity, COMP-142).

`evidence(plan)` reads the compiled problem and the verified schedule and names every
marker the plan shows. Markers are binding facts, not mere presence: a release marker means
an operation's gate sits exactly on its job release, so an executor that ignored releases
would start it early and fail the fidelity test. `REQUIRED[group]` lists the markers the
20 seeds of a group must show together, so the fidelity test cannot pass vacuously.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from _plans import GeneratedPlan

from twinflow.scheduling import (
    Assignment,
    Job,
    LockOverride,
    Machine,
    Operation,
    Precedence,
    ProcessingOption,
)
from twinflow.scheduling import calendar_math as cm
from twinflow.scheduling.calendar_math import Intervals
from twinflow.scheduling.setups import SetupTable

TOL = 1e-6

REQUIRED: dict[str, frozenset[str]] = {
    "core": frozenset(
        {
            "alternatives",
            "substitute",
            "precedence_binding",
            "release_binding",
            "ready_time_binding",
            "available_from_binding",
        }
    ),
    "calendars": frozenset(
        {"forbid_deferred", "pause_across_break", "unattended_past_calendar", "overtime"}
    ),
    "setups": frozenset(
        {
            "abs_diff",
            "mismatch",
            "family_matrix",
            "machine_function",
            "machine_state_setup",
            "detached_early",
            "available_from_binding",
        }
    ),
    "operators": frozenset(
        {"phase_setup", "phase_run", "phase_both", "pool_calendar_binding", "pool_saturated"}
    ),
    "batches": frozenset({"batch_multi", "batch_setup"}),
    "precedence": frozenset(
        {
            "min_lag_binding",
            "max_lag",
            "overlap",
            "pin_window",
            "pin_machine",
            "dag_join",
            "dag_fork",
        }
    ),
    "state": frozenset(
        {
            "in_progress",
            "paused_lock",
            "held_status",
            "held_override",
            "hold_until_binding",
            "done_predecessor",
            "done_job",
            "split",
            "ready_time_binding",
        }
    ),
    "restart": frozenset({"restart_idle"}),
}
REQUIRED["mixed"] = frozenset().union(*(REQUIRED[g] for g in REQUIRED if g != "restart"))


@dataclass(frozen=True)
class _View:
    """Lookups over one plan."""

    plan: GeneratedPlan
    by_op: Mapping[str, Assignment]
    op: Mapping[str, Operation]
    job_of: Mapping[str, Job]
    machine: Mapping[str, Machine]
    calendar: Mapping[str, Intervals]
    pool_calendar: Mapping[str, Intervals]
    setups: SetupTable
    sequence: Mapping[str, list[Assignment]]
    setup_pred: Mapping[str, str | None]

    @property
    def now(self) -> float:
        return self.plan.compiled.problem.now

    def option(self, a: Assignment) -> ProcessingOption:
        return next(o for o in self.op[a.operation_id].options if o.machine_id == a.machine_id)

    def machine_calendar(self, machine_id: str) -> Intervals:
        calendar_id = self.machine[machine_id].calendar_id
        return None if calendar_id is None else self.calendar[calendar_id]

    def effective(self, a: Assignment) -> Intervals:
        intervals = self.machine_calendar(a.machine_id)
        for need in self.op[a.operation_id].operator_needs:
            intervals = cm.intersect(intervals, self.pool_calendar[need.pool_id])
        return intervals

    def gate(self, a: Assignment) -> float:
        """The material gate of the assignment (setup_end when detached, else start)."""
        return a.setup_end if self.op[a.operation_id].setup_detached else a.start

    def setup(self, a: Assignment) -> float:
        return a.setup_end - a.start

    def link_base(self, link: Precedence) -> float | None:
        """pred.end + min_lag, or the transfer-lot time + min_lag; None if not assigned."""
        pred = self.by_op.get(link.operation_id)
        if pred is None:
            return None
        if link.overlap_quantity is None:
            return pred.end + link.min_lag
        option = self.option(pred)
        transfer = (
            pred.setup_end + option.fixed_seconds + link.overlap_quantity * option.unit_seconds
        )
        return transfer + link.min_lag


def _view(plan: GeneratedPlan) -> _View:
    problem = plan.compiled.problem
    calendars = {c.id: c.intervals for c in problem.calendars}
    sequence: dict[str, list[Assignment]] = defaultdict(list)
    for a in plan.schedule.assignments:
        sequence[a.machine_id].append(a)
    for items in sequence.values():
        items.sort(key=lambda a: (a.start, a.end, a.operation_id))
    return _View(
        plan=plan,
        by_op={a.operation_id: a for a in plan.schedule.assignments},
        op={op.id: op for job in problem.jobs for op in job.operations},
        job_of={op.id: job for job in problem.jobs for op in job.operations},
        machine={m.id: m for m in problem.machines},
        calendar=calendars,
        pool_calendar={
            p.id: calendars[p.calendar_id] if p.calendar_id else None
            for p in problem.operator_pools
        },
        setups=SetupTable.for_problem(problem),
        sequence=sequence,
        setup_pred=_setup_predecessors(sequence),
    )


def _setup_predecessors(sequence: Mapping[str, list[Assignment]]) -> dict[str, str | None]:
    """The verifier's rule: the previous unit on the machine; batch members share it."""
    predecessor: dict[str, str | None] = {}
    for items in sequence.values():
        last: Assignment | None = None
        for a in items:
            if last is None:
                predecessor[a.operation_id] = None
            elif a.batch_id is not None and a.batch_id == last.batch_id:
                predecessor[a.operation_id] = predecessor[last.operation_id]
            else:
                predecessor[a.operation_id] = last.operation_id
            last = a
    return predecessor


# --- markers ----------------------------------------------------------------------------------


def _core_markers(v: _View, a: Assignment) -> Iterator[str]:
    op, job = v.op[a.operation_id], v.job_of[a.operation_id]
    option = v.option(a)
    if len(op.options) > 1:
        yield "alternatives"
    if option.work_center_id not in (None, op.work_center_id):
        yield "substitute"
    gate = v.gate(a)
    if job.release > max(v.now, op.ready_time) + TOL and abs(gate - job.release) <= TOL:
        yield "release_binding"
    if op.ready_time > max(v.now, job.release) + TOL and abs(gate - op.ready_time) <= TOL:
        yield "ready_time_binding"
    available = v.machine[a.machine_id].state.available_from
    first = v.sequence[a.machine_id][0].operation_id == a.operation_id
    if first and available > v.now + TOL and abs(a.start - available) <= TOL:
        yield "available_from_binding"


def _link_markers(v: _View, a: Assignment) -> Iterator[str]:
    op = v.op[a.operation_id]
    gate = v.gate(a)
    if len(op.predecessors) >= 2:
        yield "dag_join"
    for link in op.predecessors:
        pred_op = v.op.get(link.operation_id)
        if pred_op is not None and pred_op.status.state == "done":
            yield "done_predecessor"
        base = v.link_base(link)
        if base is None:
            continue
        binding = abs(gate - base) <= TOL
        if binding and link.overlap_quantity is None:
            yield "precedence_binding"
        if binding and link.min_lag > 0:
            yield "min_lag_binding"
        if link.max_lag is not None:
            yield "max_lag"
        if link.overlap_quantity is not None and a.start < v.by_op[link.operation_id].end - TOL:
            yield "overlap"


def _calendar_markers(v: _View, a: Assignment) -> Iterator[str]:
    calendar = v.effective(a)
    if calendar is None:
        return
    op = v.op[a.operation_id]
    run = v.option(a).fixed_seconds + op.quantity * v.option(a).unit_seconds
    if op.shift_crossing == "pause" and a.end - a.start > v.setup(a) + run + TOL:
        yield "pause_across_break"
    if op.shift_crossing == "finish_unattended" and not cm.fits_within(calendar, a.start, a.end):
        yield "unattended_past_calendar"
    opens = {start for start, _ in calendar}
    if op.shift_crossing == "forbid" and a.start in opens and a.start > v.gate(a) - TOL:
        earlier = v.sequence[a.machine_id]
        index = [x.operation_id for x in earlier].index(a.operation_id)
        machine_free = earlier[index - 1].end if index else v.now
        if a.start > machine_free + TOL:
            yield "forbid_deferred"
    calendar_id = v.machine[a.machine_id].calendar_id
    if calendar_id is not None:
        overtime = next(
            c for c in v.plan.compiled.problem.calendars if c.id == calendar_id
        ).overtime
        if any(w.start < a.end and a.start < w.end for w in overtime):
            yield "overtime"


def _setup_markers(v: _View, a: Assignment) -> Iterator[str]:
    setup = v.setup(a)
    function = v.setups.function_for(a.machine_id)
    if setup <= TOL or function is None:
        return
    op, machine = v.op[a.operation_id], v.machine[a.machine_id]
    prev_id = v.setup_pred[a.operation_id]
    prev_params = machine.state.last_setup_params if prev_id is None else v.op[prev_id].setup_params
    if machine.setup_function_id is not None:
        yield "machine_function"
    if prev_id is None and (machine.state.last_setup_params or machine.state.last_setup_family):
        yield "machine_state_setup"
    terms = {t.param: t.kind for t in function.terms}
    by_params = bool(terms) and all(p in prev_params and p in op.setup_params for p in terms)
    if by_params:
        yield from (
            kind for param, kind in terms.items() if prev_params[param] != op.setup_params[param]
        )
    elif function.family_matrix and op.setup_family is not None:
        yield "family_matrix"
    if op.setup_detached and a.start < _material_ready(v, a) - TOL:
        yield "detached_early"


def _material_ready(v: _View, a: Assignment) -> float:
    op, job = v.op[a.operation_id], v.job_of[a.operation_id]
    bases = (v.link_base(link) for link in op.predecessors)
    return max([job.release, op.ready_time, *(b for b in bases if b is not None)])


def _operator_markers(v: _View, a: Assignment) -> Iterator[str]:
    for need in v.op[a.operation_id].operator_needs:
        span = {"setup": (a.start, a.setup_end), "run": (a.setup_end, a.end)}.get(
            need.phase, (a.start, a.end)
        )
        if span[1] > span[0] + TOL:
            yield f"phase_{need.phase}"
        pool_calendar = v.pool_calendar[need.pool_id]
        if pool_calendar is None or a.start not in {s for s, _ in pool_calendar}:
            continue
        machine_calendar = v.machine_calendar(a.machine_id)
        before = a.start - 1.0
        if cm.next_working(machine_calendar, before) == before and a.start > v.gate(a) - TOL:
            yield "pool_calendar_binding"


def _pool_saturated(v: _View) -> Iterator[str]:
    spans: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for a in v.plan.schedule.assignments:
        for need in v.op[a.operation_id].operator_needs:
            start, end = {"setup": (a.start, a.setup_end), "run": (a.setup_end, a.end)}.get(
                need.phase, (a.start, a.end)
            )
            if end > start:
                spans[need.pool_id] += [(end, -need.quantity), (start, need.quantity)]
    sizes = {p.id: p.size for p in v.plan.compiled.problem.operator_pools}
    for pool_id, events in spans.items():
        level, peak = 0, 0
        for _, delta in sorted(events):
            level += delta
            peak = max(peak, level)
        if len(events) >= 4 and peak == sizes[pool_id]:
            yield "pool_saturated"


def _batch_markers(v: _View) -> Iterator[str]:
    groups: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for a in v.plan.schedule.assignments:
        if a.batch_id is not None:
            groups[a.machine_id, a.batch_id].append(a)
    for members in groups.values():
        if len(members) >= 2:
            yield "batch_multi"
            if v.setup(members[0]) > TOL:
                yield "batch_setup"


def _plan_markers(v: _View) -> Iterator[str]:
    """Markers about the problem and the plan as a whole."""
    plan, problem = v.plan, v.plan.compiled.problem
    for op_id, op in v.op.items():
        if op.status.state == "in_progress" and op_id in v.by_op:
            yield "in_progress"
        if op.pin is not None and op_id in v.by_op:
            yield "pin_window" if op.pin.latest_start is not None else "pin_machine"
        same_job = [o for o in v.job_of[op_id].operations if op_id in _pred_ids(o)]
        if len(same_job) >= 2:
            yield "dag_fork"
    if any(job.status == "held" for job in problem.jobs):
        yield "held_status"
    if plan.compiled.held_job_ids:
        yield "held_override"
    if any(job.status == "done" for job in problem.jobs):
        yield "done_job"
    if plan.compiled.split_map and any("~" in op_id for op_id in v.by_op):
        yield "split"
    yield from _override_markers(v)
    yield from _restart_markers(v)


def _pred_ids(op: Operation) -> set[str]:
    return {link.operation_id for link in op.predecessors}


def _override_markers(v: _View) -> Iterator[str]:
    request = v.plan.request
    for override in request.overrides:
        if isinstance(override, LockOverride) and override.operation_id in v.by_op:
            a, op = v.by_op[override.operation_id], v.op[override.operation_id]
            if len(op.options) == 1 and op.operator_needs and a.start > v.now + TOL:
                yield "paused_lock"
        if override.kind == "hold" and override.until is not None:
            gates = [
                v.gate(a) for op_id, a in v.by_op.items() if v.job_of[op_id].id == override.job_id
            ]
            if any(abs(g - override.until) <= TOL for g in gates):
                yield "hold_until_binding"


def _restart_markers(v: _View) -> Iterator[str]:
    for machine_id, items in v.sequence.items():
        rule = v.machine[machine_id].restart
        if rule is None:
            continue
        free = max(v.now, v.machine[machine_id].state.available_from)
        for a in items:
            if a.start - free > rule.idle_threshold_seconds + TOL:
                yield "restart_idle"
            free = max(free, a.end)


def evidence(plan: GeneratedPlan) -> frozenset[str]:
    """Every marker `plan` shows (see REQUIRED for the vocabulary)."""
    v = _view(plan)
    found: set[str] = set()
    for a in plan.schedule.assignments:
        if v.op[a.operation_id].status.state == "in_progress":
            continue
        for markers in (_core_markers, _link_markers, _calendar_markers):
            found.update(markers(v, a))
        found.update(_setup_markers(v, a))
        found.update(_operator_markers(v, a))
    found.update(_pool_saturated(v))
    found.update(_batch_markers(v))
    found.update(_plan_markers(v))
    return frozenset(found)
