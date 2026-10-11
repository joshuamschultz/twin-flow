"""Problem builder: ERP snapshot + planning profile -> TF-002 `SolveRequest` (COMP-116).

`build_problem_from_erp(snapshot, profile) -> BuildResult` (D-111, D-114):

1. `validate_snapshot`; any hard issue blocks: no request, no problem digest.
2. Open orders become jobs (`_jobs.py`), their in-plant ops become operations
   (`_operations.py`); material readiness (COMP-115, D-122) runs in job priority order.
3. Active machines, work centers, calendars with priced overtime (COMP-114, COMP-126,
   D-125; `_machine_calendars.py`), operator pools and the profile's setup functions
   complete the problem; every collection is sorted by id.
4. Problem id = `"erp-" + sha256("\\n".join([snapshot digest, profile digest,
   BUILDER_VERSION]))[:16]`; problem digest = SHA-256 of the request's canonical JSON. The
   request is round-tripped through TF-002 `parse_request`; a failure is a builder bug and
   becomes a hard `builder_invariant` issue (never an exception).

Issues from the validator, material readiness and the builder are merged in that order. A
later source's issue is dropped when an earlier source already reported the same code for the
same lineage (or path, when there is no lineage); the result is sorted by (severity with
hard first, path, code).

`build_rush_job(snapshot, profile, rush)` applies the same job rules to an item's standard
routing (REQ-163). Pure and deterministic: no clock read.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable, Iterable
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from twinflow.erp._checks import issue
from twinflow.erp._jobs import JobHead, build_job, job_heads, jobs_order, running_machines
from twinflow.erp._machine_calendars import machine_calendars
from twinflow.erp._operations import (
    ShopIndex,
    build_shop_index,
    make_operation,
    processing_options,
    walk_route,
)
from twinflow.erp.issues import ErpIssue
from twinflow.erp.materials import material_ready_times
from twinflow.erp.profile import PlanningProfile, profile_digest
from twinflow.erp.schema import ErpSnapshot, Lineage, Routing
from twinflow.erp.validate import validate_snapshot
from twinflow.scheduling import parse_request
from twinflow.scheduling.model import (
    BatchCapacity,
    Id,
    Job,
    Machine,
    MachineState,
    OperationStatus,
    OperatorPool,
    PosFloat,
    Problem,
    SetupFunction,
    SolveRequest,
    WorkCenter,
)

__all__ = [
    "BUILDER_VERSION",
    "BuildResult",
    "RushOrder",
    "build_problem_from_erp",
    "build_rush_job",
]

BUILDER_VERSION = "erp-build-1"
REQUEST_SCHEMA_VERSION: Literal["2.0"] = "2.0"

_SEVERITY_RANK = {"hard": 0, "soft": 1}


class BuildResult(BaseModel):
    """The outcome of one build: a ready request, or the hard issues that block it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ready", "blocked"]
    issues: tuple[ErpIssue, ...]
    request: SolveRequest | None
    problem_digest: str | None
    snapshot_digest: str
    profile_digest: str
    builder_version: str = BUILDER_VERSION
    counts: dict[str, int]
    lineage_index: dict[str, Lineage]


class RushOrder(BaseModel):
    """A new order to insert into a plan; built from the item's standard routing."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    order_id: Id
    item_id: Id
    quantity: PosFloat
    due: AwareDatetime
    customer_id: Id | None = None
    routing_id: Id | None = None


def build_problem_from_erp(snapshot: ErpSnapshot, profile: PlanningProfile) -> BuildResult:
    """Build the TF-002 request for every open order of `snapshot` under `profile`."""
    report = validate_snapshot(snapshot, profile)
    digests = (report.snapshot_digest, profile_digest(profile))
    if report.hard_count:
        return _blocked(digests, report.issues)
    try:
        return _build(snapshot, profile, digests, report.issues)
    except ValidationError:
        found = issue("builder_invariant", "$", None)
        return _blocked(digests, _merge(report.issues, (found,)))


def build_rush_job(
    snapshot: ErpSnapshot, profile: PlanningProfile, rush: RushOrder
) -> Job | tuple[ErpIssue, ...]:
    """The rush order as a TF-002 job, or `(no_routing,)` when the item has no usable routing.

    `routing_id` picks a routing of the item; without it the item's smallest routing id wins.
    The job takes the rush class, `release` 0 and `due` = real seconds from `as_of`.
    """
    routing = _rush_routing(snapshot, rush)
    if routing is None:
        return (issue("no_routing", "$.routings", None),)
    shop = build_shop_index(snapshot, profile.erp)
    walk = walk_route(routing.operations)
    operations = []
    for step in walk.steps:
        options = processing_options(step.op, shop)
        if not options:
            return (issue("no_active_option", "$.routings", step.op.lineage),)
        operations.append(
            make_operation(
                rush.order_id,
                step,
                options,
                quantity=rush.quantity,
                batch_default=rush.item_id,
                ready_time=step.queue,
                status=OperationStatus(),
                policy=profile.erp,
            )
        )
    # Timestamps, not datetime subtraction: two datetimes sharing one tzinfo subtract as
    # wall clock, which is wrong across a DST change.
    due = rush.due.timestamp() - snapshot.as_of.timestamp() - walk.due_lead
    return Job(
        id=rush.order_id,
        weight_class=profile.erp.rush_class,
        release=walk.release_lead,
        due=due,
        operations=tuple(operations),
    )


# --- build steps -------------------------------------------------------------------------------


def _build(
    snapshot: ErpSnapshot,
    profile: PlanningProfile,
    digests: tuple[str, str],
    validator_issues: tuple[ErpIssue, ...],
) -> BuildResult:
    """Steps 2 to 11 on a snapshot the validator passed."""
    policy = profile.erp
    shop = build_shop_index(snapshot, policy)
    heads = job_heads(snapshot, policy)
    plan = material_ready_times(snapshot, jobs_order(heads), policy)
    running = running_machines(heads, shop)
    jobs: list[Job] = []
    builder_issues: list[ErpIssue] = []
    for head in heads:
        job, op_issues = build_job(head, plan, shop, policy, running)
        jobs.append(job)
        builder_issues.extend((*head.issues, *op_issues))
    problem = _problem(snapshot, profile, digests, jobs, heads, shop)
    request = SolveRequest(
        schema_version=REQUEST_SCHEMA_VERSION, problem=problem, profile=profile.scheduling
    )
    issues = _merge(validator_issues, plan.issues, builder_issues)
    dumped = request.model_dump(mode="json")
    if parse_request(dumped) != request:
        return _blocked(digests, _merge(issues, (issue("builder_invariant", "$", None),)))
    return BuildResult(
        status="ready",
        issues=issues,
        request=request,
        problem_digest=_sha256_json(dumped),
        snapshot_digest=digests[0],
        profile_digest=digests[1],
        counts=_counts(problem, heads),
        lineage_index=_lineage_index(heads),
    )


def _problem(
    snapshot: ErpSnapshot,
    profile: PlanningProfile,
    digests: tuple[str, str],
    jobs: list[Job],
    heads: list[JobHead],
    shop: ShopIndex,
) -> Problem:
    """Assemble the TF-002 problem; every collection sorted by id."""
    machine_ids_to_calendar, calendars = machine_calendars(snapshot, profile)
    machines = _machines(shop, machine_ids_to_calendar, jobs, heads)
    functions = {function.id: function for function in profile.erp.setup_functions.values()}
    return Problem(
        id=_problem_id(digests),
        now=0.0,
        jobs=tuple(sorted(jobs, key=lambda job: job.id)),
        machines=machines,
        work_centers=_work_centers(snapshot, profile.erp.setup_functions),
        calendars=calendars,
        operator_pools=tuple(
            OperatorPool(id=pool.id, size=pool.size, calendar_id=pool.calendar_id)
            for pool in sorted(snapshot.labor_pools, key=lambda pool: pool.id)
        ),
        setup_functions=tuple(functions[key] for key in sorted(functions)),
    )


def _machines(
    shop: ShopIndex,
    machine_calendars: dict[str, str | None],
    jobs: Iterable[Job],
    heads: Iterable[JobHead],
) -> tuple[Machine, ...]:
    """Active machines with calendar, batch capacity and the end of their running op."""
    busy_until: dict[str, float] = {}
    for job in jobs:
        for op in job.operations:
            status = op.status
            if status.machine_id is not None and status.remaining_seconds is not None:
                end = max(busy_until.get(status.machine_id, 0.0), status.remaining_seconds)
                busy_until[status.machine_id] = end
    batches: dict[str, BatchCapacity] = {}
    for head in heads:
        for step in head.walk.steps:
            if step.op.batch is not None:
                spec = step.op.batch
                capacity = BatchCapacity(capacity=spec.capacity, unit=spec.unit)
                batches.setdefault(step.op.work_center_id, capacity)
    return tuple(
        Machine(
            id=machine.id,
            work_center_id=machine.work_center_id,
            calendar_id=machine_calendars[machine.id],
            batch=batches.get(machine.work_center_id),
            state=MachineState(available_from=busy_until.get(machine.id, 0.0)),
        )
        for machine in sorted(shop.machines.values(), key=lambda m: m.id)
        if machine.active
    )


def _work_centers(
    snapshot: ErpSnapshot, setup_functions: dict[str, SetupFunction]
) -> tuple[WorkCenter, ...]:
    ids = sorted({wc.id for wc in snapshot.work_centers})
    return tuple(
        WorkCenter(
            id=wc_id,
            setup_function_id=setup_functions[wc_id].id if wc_id in setup_functions else None,
        )
        for wc_id in ids
    )


def _rush_routing(snapshot: ErpSnapshot, rush: RushOrder) -> Routing | None:
    """The named routing when it belongs to the item, else the item's smallest routing id."""
    routings = sorted(
        (routing for routing in snapshot.routings if routing.item_id == rush.item_id),
        key=lambda routing: routing.id,
    )
    if rush.routing_id is None:
        return routings[0] if routings else None
    return next((routing for routing in routings if routing.id == rush.routing_id), None)


# --- envelope ----------------------------------------------------------------------------------


def _problem_id(digests: tuple[str, str]) -> str:
    joined = "\n".join([*digests, BUILDER_VERSION])
    return "erp-" + hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _sha256_json(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _counts(problem: Problem, heads: Iterable[JobHead]) -> dict[str, int]:
    done = ("complete", "cancelled")
    return {
        "jobs": len(problem.jobs),
        "operations": sum(len(job.operations) for job in problem.jobs),
        "machines": len(problem.machines),
        "held_jobs": sum(1 for job in problem.jobs if job.status == "held"),
        "done_operations": sum(
            1 for head in heads for op in head.order.operations if op.status in done
        ),
    }


def _lineage_index(heads: Iterable[JobHead]) -> dict[str, Lineage]:
    """Lineage of every job (its order) and every operation (its order operation)."""
    index: dict[str, Lineage] = {}
    for head in heads:
        index[head.order.id] = head.order.lineage
        for step in head.walk.steps:
            index[f"{head.order.id}:{step.op.op_id}"] = step.op.lineage
    return dict(sorted(index.items()))


def _blocked(digests: tuple[str, str], issues: tuple[ErpIssue, ...]) -> BuildResult:
    return BuildResult(
        status="blocked",
        issues=issues,
        request=None,
        problem_digest=None,
        snapshot_digest=digests[0],
        profile_digest=digests[1],
        counts=dict.fromkeys(("jobs", "operations", "machines", "held_jobs", "done_operations"), 0),
        lineage_index={},
    )


def _merge(*sources: Iterable[ErpIssue]) -> tuple[ErpIssue, ...]:
    """Concatenate sources; drop a later source's repeat of an earlier source's issue."""
    kept: list[ErpIssue] = []
    seen: set[Hashable] = set()
    for source in sources:
        found = list(source)
        kept.extend(found_issue for found_issue in found if _dedupe_key(found_issue) not in seen)
        seen.update(_dedupe_key(found_issue) for found_issue in found)
    return tuple(sorted(kept, key=_sort_key))


def _dedupe_key(found: ErpIssue) -> Hashable:
    return (found.code, found.lineage if found.lineage is not None else found.path)


def _sort_key(found: ErpIssue) -> tuple[int, str, str]:
    return (_SEVERITY_RANK[found.severity], found.path, found.code)
