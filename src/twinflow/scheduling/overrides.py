"""Override compiler: overrides and the frozen zone become plain model data (COMP-003, D-066).

`apply_overrides` is the one place the seven override kinds and the frozen zone take
effect. Solvers, the verifier and the evaluator see only the compiled problem. The
compile step is pure and deterministic: overrides apply by kind in the fixed order hold,
split, priority and expedite, exclude_machine, prefer_machine, lock, and by override id
within a kind, so the order of the request list never changes the result. The frozen
zone applies last, so a lock or hard machine removal on a frozen operation wins over it.

Target resolution: a job or operation id that was split resolves to its children; an id
of a held job resolves to nothing (the override is skipped, the job is not solved); any
other unknown id is an `override_unknown_target` issue and the override is skipped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from twinflow.scheduling.model import (
    CompiledProblem,
    ExcludeMachineOverride,
    ExpediteOverride,
    HoldOverride,
    Job,
    LockOverride,
    Operation,
    Override,
    Pin,
    Precedence,
    PreferMachineOverride,
    PreviousAssignment,
    PriorityOverride,
    ProcessingOption,
    ScheduleIssue,
    SolveRequest,
    SplitOverride,
)

_KIND_RANK: dict[str, int] = {
    "hold": 0,
    "split": 1,
    "priority": 2,
    "expedite": 2,
    "exclude_machine": 3,
    "prefer_machine": 4,
    "lock": 5,
}

_MachineOverride = ExcludeMachineOverride | PreferMachineOverride


@dataclass
class _State:
    """Working state threaded through the handlers; local to one `apply_overrides` call."""

    jobs: dict[str, Job]
    op_owner: dict[str, str]
    machine_ids: frozenset[str]
    held_job_ids: list[str] = field(default_factory=list)
    held_job_set: set[str] = field(default_factory=set)
    held_op_ids: set[str] = field(default_factory=set)
    split_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    issues: list[ScheduleIssue] = field(default_factory=list)
    removed_by: dict[tuple[str, str], str] = field(default_factory=dict)
    locked_by: dict[str, str] = field(default_factory=dict)
    released: set[str] = field(default_factory=set)

    def report(self, code: str, path: str, message: str) -> None:
        self.issues.append(ScheduleIssue(code=code, path=path, message=message))


def apply_overrides(request: SolveRequest) -> CompiledProblem:
    """Compile the request's overrides and frozen zone into the problem (D-066).

    Effective job weights (explicit `weight_override`, else the profile's class weight)
    are written into every compiled `Job.weight_override`, and the effective horizon (the
    earlier of the problem's and the profile's `horizon_end`) into the compiled
    `Problem.horizon_end`. The request is not changed.
    """
    problem = request.problem
    state = _State(
        jobs={job.id: job for job in problem.jobs},
        op_owner={op.id: job.id for job in problem.jobs for op in job.operations},
        machine_ids=frozenset(machine.id for machine in problem.machines),
    )
    for index, override in _ordered(request.overrides):
        _apply_one(state, override, f"overrides[{index}]")
    _report_held_predecessors(state, problem.jobs)
    _apply_frozen_zone(state, request)
    weights = request.profile.class_weights
    jobs = tuple(_with_effective_weight(job, weights) for job in _final_jobs(state, problem.jobs))
    horizon = _effective_horizon(problem.horizon_end, request.profile.horizon_end)
    return CompiledProblem(
        problem=problem.model_copy(update={"jobs": jobs, "horizon_end": horizon}),
        profile=request.profile,
        held_job_ids=tuple(state.held_job_ids),
        released_from_frozen=tuple(sorted(state.released)),
        split_map=state.split_map,
        override_issues=tuple(state.issues),
    )


def _ordered(overrides: Iterable[Override]) -> list[tuple[int, Override]]:
    """Overrides with their request index, sorted by kind rank, then id, then index."""
    indexed = list(enumerate(overrides))
    return sorted(indexed, key=lambda item: (_KIND_RANK[item[1].kind], item[1].id, item[0]))


def _apply_one(state: _State, override: Override, path: str) -> None:
    match override:
        case HoldOverride():
            _apply_hold(state, override, path)
        case SplitOverride():
            _apply_split(state, override, path)
        case PriorityOverride():
            _apply_priority(state, override, path)
        case ExpediteOverride():
            _apply_expedite(state, override, path)
        case ExcludeMachineOverride() | PreferMachineOverride():
            _apply_machine_override(state, override, path)
        case LockOverride():
            _apply_lock(state, override, path)


# --- target resolution ----------------------------------------------------------------------


def _job_targets(state: _State, job_id: str) -> tuple[str, ...] | None:
    """Current job ids for `job_id`: itself, its split children, () if held, None if unknown."""
    if job_id in state.jobs:
        return (job_id,)
    if job_id in state.split_map:
        return state.split_map[job_id]
    if job_id in state.held_job_set:
        return ()
    return None


def _op_targets(state: _State, op_id: str) -> tuple[str, ...] | None:
    """Current op ids for `op_id`: itself, its split children, () if held, None if unknown."""
    if op_id in state.op_owner:
        return (op_id,)
    if op_id in state.split_map:
        return state.split_map[op_id]
    if op_id in state.held_op_ids:
        return ()
    return None


def _unknown(state: _State, path: str, override_id: str, what: str) -> None:
    state.report("override_unknown_target", path, f"override {override_id}: unknown {what}")


def _operation(state: _State, op_id: str) -> Operation:
    job = state.jobs[state.op_owner[op_id]]
    return next(op for op in job.operations if op.id == op_id)


def _update_job(state: _State, job_ids: Iterable[str], change: Callable[[Job], Job]) -> None:
    for job_id in job_ids:
        state.jobs[job_id] = change(state.jobs[job_id])


def _update_op(state: _State, op_id: str, change: Callable[[Operation], Operation]) -> None:
    job = state.jobs[state.op_owner[op_id]]
    ops = tuple(change(op) if op.id == op_id else op for op in job.operations)
    state.jobs[job.id] = job.model_copy(update={"operations": ops})


# --- hold and split -------------------------------------------------------------------------


def _apply_hold(state: _State, hold: HoldOverride, path: str) -> None:
    """With `until`: release = max(release, until). Without: drop the job from this solve."""
    job = state.jobs.get(hold.job_id)
    if job is None:
        _unknown(state, path, hold.id, f"job {hold.job_id}")
        return
    if hold.until is not None:
        state.jobs[job.id] = job.model_copy(update={"release": max(job.release, hold.until)})
        return
    del state.jobs[job.id]
    state.held_job_ids.append(job.id)
    state.held_job_set.add(job.id)
    for op in job.operations:
        del state.op_owner[op.id]
        state.held_op_ids.add(op.id)


def _apply_split(state: _State, split: SplitOverride, path: str) -> None:
    """Replace the job with children `{job}~k` (k from 1), ops `{op}~k`, quantities scaled.

    A link from another job to a split op X becomes one link per child X~k with the same
    lags, so the successor waits for every child. A transfer-lot link from another job
    into the split job has no single lot to count, so it is `split_overlap_link` and the
    split is skipped.
    """
    job = state.jobs.get(split.job_id)
    if job is None:
        if _job_targets(state, split.job_id) is None:
            _unknown(state, path, split.id, f"job {split.job_id}")
        return
    overlapping = _overlap_links_into(state, job)
    if overlapping:
        message = f"split {split.id}: {', '.join(overlapping)} overlap into job {job.id}"
        state.report("split_overlap_link", path, message)
        return
    children = _split_job(job, split.fractions)
    del state.jobs[job.id]
    state.split_map[job.id] = tuple(child.id for child in children)
    for op in job.operations:
        del state.op_owner[op.id]
        state.split_map[op.id] = tuple(f"{op.id}~{k}" for k in range(1, len(children) + 1))
    _update_job(state, list(state.jobs), lambda other: _remap_external(other, state.split_map))
    for child in children:
        state.jobs[child.id] = child
        state.op_owner.update({op.id: child.id for op in child.operations})


def _overlap_links_into(state: _State, job: Job) -> list[str]:
    """Ids of ops in other jobs with a transfer-lot link to an op of `job`."""
    local = {op.id for op in job.operations}
    return [
        op.id
        for other in state.jobs.values()
        if other.id != job.id
        for op in other.operations
        if any(
            link.operation_id in local and link.overlap_quantity is not None
            for link in op.predecessors
        )
    ]


def _remap_external(job: Job, split_map: dict[str, tuple[str, ...]]) -> Job:
    """Fan every link to a split op out to its children; a job with no such link is kept."""
    if not any(link.operation_id in split_map for op in job.operations for link in op.predecessors):
        return job
    operations = tuple(
        op.model_copy(
            update={
                "predecessors": tuple(
                    link.model_copy(update={"operation_id": child})
                    for link in op.predecessors
                    for child in split_map.get(link.operation_id, (link.operation_id,))
                )
            }
        )
        for op in job.operations
    )
    return job.model_copy(update={"operations": operations})


def _split_job(job: Job, fractions: tuple[float, ...]) -> tuple[Job, ...]:
    local_ids = frozenset(op.id for op in job.operations)
    return tuple(
        job.model_copy(
            update={
                "id": f"{job.id}~{k}",
                "operations": tuple(
                    _split_operation(op, k, fraction, local_ids) for op in job.operations
                ),
            }
        )
        for k, fraction in enumerate(fractions, start=1)
    )


def _split_operation(
    op: Operation, k: int, fraction: float, local_ids: frozenset[str]
) -> Operation:
    predecessors = tuple(
        _remap_precedence(link, k, fraction, local_ids) for link in op.predecessors
    )
    return op.model_copy(
        update={
            "id": f"{op.id}~{k}",
            "quantity": op.quantity * fraction,
            "predecessors": predecessors,
        }
    )


def _remap_precedence(
    link: Precedence, k: int, fraction: float, local_ids: frozenset[str]
) -> Precedence:
    """A link inside the split job points at the same child, and its transfer lot scales
    with the child's lot; links to other jobs stay (a later split of that job fans them out)."""
    if link.operation_id not in local_ids:
        return link
    overlap = None if link.overlap_quantity is None else link.overlap_quantity * fraction
    return link.model_copy(
        update={"operation_id": f"{link.operation_id}~{k}", "overlap_quantity": overlap}
    )


def _report_held_predecessors(state: _State, jobs: tuple[Job, ...]) -> None:
    """`held_predecessor`: an op still solved waits on an unfinished op a hold removed."""
    held_pending = {
        op.id
        for job in jobs
        if job.id in state.held_job_set
        for op in job.operations
        if op.status.state != "done"
    }
    for j, job in enumerate(jobs):
        if job.id in state.held_job_set or job.status != "open":
            continue
        for o, op in enumerate(job.operations):
            if op.status.state == "done":
                continue
            for k, link in enumerate(op.predecessors):
                if link.operation_id in held_pending:
                    path = f"problem.jobs[{j}].operations[{o}].predecessors[{k}]"
                    message = f"predecessor {link.operation_id} of {op.id} belongs to a held job"
                    state.report("held_predecessor", path, message)


# --- priority and expedite ------------------------------------------------------------------


def _apply_priority(state: _State, priority: PriorityOverride, path: str) -> None:
    """Set the class (dropping any explicit weight, so the class weight applies) or weight."""
    targets = _job_targets(state, priority.job_id)
    if targets is None:
        _unknown(state, path, priority.id, f"job {priority.job_id}")
        return
    update: dict[str, object] = (
        {"weight_class": priority.weight_class, "weight_override": None}
        if priority.weight_class is not None
        else {"weight_override": priority.weight}
    )
    _update_job(state, targets, lambda job: job.model_copy(update=update))


def _apply_expedite(state: _State, expedite: ExpediteOverride, path: str) -> None:
    """Class 4; due becomes min(due, given due), or the given due when the job has none."""
    targets = _job_targets(state, expedite.job_id)
    if targets is None:
        _unknown(state, path, expedite.id, f"job {expedite.job_id}")
        return
    _update_job(state, targets, lambda job: _expedited(job, expedite.due))


def _expedited(job: Job, due: float | None) -> Job:
    if due is not None and job.due is not None:
        due = min(job.due, due)
    return job.model_copy(update={"weight_class": 4, "due": job.due if due is None else due})


# --- exclude_machine and prefer_machine -----------------------------------------------------


def _apply_machine_override(state: _State, override: _MachineOverride, path: str) -> None:
    """Resolve the op or job target, then apply the override to each target operation."""
    if override.machine_id not in state.machine_ids:
        _unknown(state, path, override.id, f"machine {override.machine_id}")
        return
    op_ids = _machine_override_ops(state, override)
    if op_ids is None:
        target = override.operation_id or override.job_id
        _unknown(state, path, override.id, f"target {target}")
        return
    for op_id in op_ids:
        _apply_machine_override_to_op(state, override, op_id, path)


def _machine_override_ops(state: _State, override: _MachineOverride) -> tuple[str, ...] | None:
    if override.operation_id is not None:
        return _op_targets(state, override.operation_id)
    job_ids = _job_targets(state, override.job_id or "")
    if job_ids is None:
        return None
    return tuple(op.id for job_id in job_ids for op in state.jobs[job_id].operations)


def _apply_machine_override_to_op(
    state: _State, override: _MachineOverride, op_id: str, path: str
) -> None:
    options = _operation(state, op_id).options
    if override.hardness == "soft":
        penalised = _soft_penalised(override, options)
        _set_options(state, op_id, _with_penalty(options, penalised, override.penalty or 0.0))
        return
    kept = _hard_kept(override, options)
    if not kept:
        state.report(
            "override_conflict",
            path,
            f"override {override.id} leaves operation {op_id} with no machine option",
        )
        return
    for option in options:
        if option not in kept:
            state.removed_by[(op_id, option.machine_id)] = override.id
    _set_options(state, op_id, kept)


def _hard_kept(
    override: _MachineOverride, options: tuple[ProcessingOption, ...]
) -> tuple[ProcessingOption, ...]:
    """Exclude keeps every option but the machine; prefer keeps only the machine."""
    keep_match = isinstance(override, PreferMachineOverride)
    return tuple(o for o in options if (o.machine_id == override.machine_id) == keep_match)


def _soft_penalised(override: _MachineOverride, options: tuple[ProcessingOption, ...]) -> set[str]:
    """Exclude penalises the machine; prefer penalises every other machine."""
    if isinstance(override, ExcludeMachineOverride):
        return {override.machine_id}
    return {o.machine_id for o in options if o.machine_id != override.machine_id}


def _with_penalty(
    options: tuple[ProcessingOption, ...], machine_ids: set[str], penalty: float
) -> tuple[ProcessingOption, ...]:
    return tuple(
        o.model_copy(update={"preference_cost": o.preference_cost + penalty})
        if o.machine_id in machine_ids
        else o
        for o in options
    )


def _set_options(state: _State, op_id: str, options: tuple[ProcessingOption, ...]) -> None:
    _update_op(state, op_id, lambda op: op.model_copy(update={"options": options}))


# --- lock -----------------------------------------------------------------------------------


def _apply_lock(state: _State, lock: LockOverride, path: str) -> None:
    """Pin the operation to the machine and/or the exact start; conflicts skip the lock."""
    if lock.machine_id is not None and lock.machine_id not in state.machine_ids:
        _unknown(state, path, lock.id, f"machine {lock.machine_id}")
        return
    targets = _op_targets(state, lock.operation_id)
    if targets is None:
        _unknown(state, path, lock.id, f"operation {lock.operation_id}")
        return
    conflict = _lock_conflict(state, lock, targets)
    if conflict is not None:
        state.report("override_conflict", path, conflict)
        return
    for op_id in targets:
        state.locked_by[op_id] = lock.id
        _update_op(state, op_id, lambda op: op.model_copy(update={"pin": _lock_pin(lock)}))


def _lock_conflict(state: _State, lock: LockOverride, targets: tuple[str, ...]) -> str | None:
    """Why the lock cannot apply, naming every override id involved; None when it can."""
    if len(targets) > 1:
        return f"lock {lock.id} targets split operation {lock.operation_id}"
    for op_id in targets:
        if op_id in state.locked_by:
            return f"lock {lock.id} conflicts with lock {state.locked_by[op_id]} on {op_id}"
        remover = _remover(state, op_id, lock.machine_id)
        if remover is not None:
            return f"lock {lock.id} uses machine {lock.machine_id} removed by {remover}"
    return None


def _remover(state: _State, op_id: str, machine_id: str | None) -> str | None:
    """The id of the hard override that removed `machine_id` from the op, if any."""
    if machine_id is None:
        return None
    return state.removed_by.get((op_id, machine_id))


def _lock_pin(lock: LockOverride) -> Pin:
    return Pin(machine_id=lock.machine_id, earliest_start=lock.start, latest_start=lock.start)


# --- frozen zone ----------------------------------------------------------------------------


def _apply_frozen_zone(state: _State, request: SolveRequest) -> None:
    """Pin previous-plan ops that start before `until` to their machine, start +- tolerance."""
    zone = request.profile.frozen_zone
    if zone is None:
        return
    if request.previous_plan is None:
        state.report(
            "frozen_requires_previous_plan",
            "profile.frozen_zone",
            "a frozen zone needs a previous plan",
        )
        return
    for assignment in request.previous_plan.assignments:
        if assignment.start < zone.until:
            _freeze(state, assignment, zone.tolerance)


def _freeze(state: _State, assignment: PreviousAssignment, tolerance: float) -> None:
    """Pin one op; a lock on it or a hard removal of its machine wins and releases it.

    Done ops are skipped; so are in-progress ops, which their status already fixes at `now`.
    """
    op_id = assignment.operation_id
    if op_id not in state.op_owner or _operation(state, op_id).status.state != "pending":
        return
    if op_id in state.locked_by or (op_id, assignment.machine_id) in state.removed_by:
        state.released.add(op_id)
        return
    pin = Pin(
        machine_id=assignment.machine_id,
        earliest_start=assignment.start - tolerance,
        latest_start=assignment.start + tolerance,
    )
    _update_op(state, op_id, lambda op: op.model_copy(update={"pin": pin}))


# --- assembly -------------------------------------------------------------------------------


def _final_jobs(state: _State, original: tuple[Job, ...]) -> list[Job]:
    """Jobs in input order, each split job replaced in place by its children."""
    ordered: list[Job] = []
    for job in original:
        for job_id in _job_targets(state, job.id) or ():
            ordered.append(state.jobs[job_id])
    return ordered


def _effective_horizon(*ends: float | None) -> float | None:
    """The earliest of the given horizon ends; None when none is set."""
    return min((end for end in ends if end is not None), default=None)


def _with_effective_weight(job: Job, class_weights: dict[int, float]) -> Job:
    weight = job.weight_override
    if weight is None:
        weight = class_weights[job.weight_class]
    return job.model_copy(update={"weight_override": weight})
