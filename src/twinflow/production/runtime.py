"""Public orchestration boundary for scheduling and attributed state ledgers."""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TypeVar

from . import scheduling
from .contracts import (
    OperationAssignment,
    ProductionJob,
    ProductionOperation,
    ProductionProblem,
    ProductionSchedule,
    ProductionVerification,
    RecipeAlternative,
    ScenarioCohort,
)
from .external import ExternalLedger, ExternalOperation, VendorReceipt
from .lots import LotBalance, LotLedger
from .materials import (
    MaterialConsumption,
    MaterialLedger,
    MaterialRequirement,
    MaterialReservation,
)
from .qualification import (
    QualificationEvent,
    QualificationState,
    QualificationTrial,
)


class ProductionRuntimeError(ValueError):
    """An integrated workflow cannot be composed without changing its meaning."""


_MaterialValue = TypeVar("_MaterialValue")


@dataclass(frozen=True, slots=True)
class QualificationAttempt:
    passed: bool
    measurements: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "measurements", MappingProxyType(dict(self.measurements)))


@dataclass(frozen=True, slots=True)
class QualificationWorkflow:
    id: str
    state: QualificationState
    sample_operation: ProductionOperation
    test_operations: tuple[ProductionOperation, ...]
    adjustment_operation: ProductionOperation
    attempts: tuple[QualificationAttempt, ...]
    sample_uom: str = "piece"
    sample_disposition: str = "retained"

    def __post_init__(self) -> None:
        object.__setattr__(self, "test_operations", tuple(self.test_operations))
        object.__setattr__(self, "attempts", tuple(self.attempts))
        if not self.sample_uom.strip():
            raise ValueError("sample_uom is required")
        if self.sample_disposition not in {"retained", "scrapped", "consumed"}:
            raise ValueError("unsupported sample disposition")


@dataclass(frozen=True, slots=True)
class ExternalReceiptPlan:
    accepted_qty: float
    rejected_qty: float
    lost_qty: float
    at: float
    event_id: str

    def __post_init__(self) -> None:
        values = (self.accepted_qty, self.rejected_qty, self.lost_qty, self.at)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
            for value in values
        ):
            raise ValueError("external receipt quantities and time must be finite and non-negative")
        if not self.event_id.strip():
            raise ValueError("external receipt event_id is required")


@dataclass(frozen=True, slots=True)
class ExternalWorkflow:
    operation: ExternalOperation
    source_operation_id: str
    downstream_operation: ProductionOperation
    lot_id: str
    qty: float
    uom: str
    receipts: tuple[ExternalReceiptPlan, ...]
    vendor_operation: ProductionOperation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipts", tuple(self.receipts))
        if not self.source_operation_id or not self.lot_id or not self.uom.strip():
            raise ValueError("external source, lot, and unit are required")
        if (
            isinstance(self.qty, bool)
            or not isinstance(self.qty, (int, float))
            or not math.isfinite(float(self.qty))
            or self.qty <= 0
        ):
            raise ValueError("external workflow qty must be finite and positive")
        event_ids = [item.event_id for item in self.receipts]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("external receipt event IDs must be unique within a workflow")


@dataclass(frozen=True, slots=True)
class RuntimeInputs:
    lot_ledger: LotLedger
    material_ledger: MaterialLedger
    external_ledger: ExternalLedger
    qualification_workflows: tuple[QualificationWorkflow, ...] = ()
    external_workflows: tuple[ExternalWorkflow, ...] = ()
    material_requirements: Mapping[str, tuple[MaterialRequirement, ...]] = field(
        default_factory=dict
    )
    material_consumptions: Mapping[str, tuple[MaterialConsumption, ...]] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "qualification_workflows", tuple(self.qualification_workflows))
        object.__setattr__(self, "external_workflows", tuple(self.external_workflows))
        object.__setattr__(
            self,
            "material_requirements",
            MappingProxyType(
                {key: tuple(value) for key, value in (self.material_requirements or {}).items()}
            ),
        )
        object.__setattr__(
            self,
            "material_consumptions",
            MappingProxyType(
                {key: tuple(value) for key, value in (self.material_consumptions or {}).items()}
            ),
        )
        qualification_ids = [item.id for item in self.qualification_workflows]
        external_ids = [item.operation.operation_id for item in self.external_workflows]
        if any(not item for item in qualification_ids):
            raise ValueError("qualification workflow IDs are required")
        if len(set(qualification_ids)) != len(qualification_ids):
            raise ValueError("qualification workflow IDs must be unique")
        if len(set(external_ids)) != len(external_ids):
            raise ValueError("external operation IDs must be unique")


@dataclass(frozen=True, slots=True)
class QualificationRun:
    workflow_id: str
    trial: QualificationTrial
    sample_operation_id: str
    test_operation_ids: tuple[str, ...]
    adjustment_operation_id: str | None
    result: QualificationEvent


@dataclass(frozen=True, slots=True)
class ExternalReceiptRun:
    receipt: VendorReceipt
    ready_lot_id: str | None
    downstream_operation_id: str | None


@dataclass(frozen=True, slots=True)
class QualificationSampleAccount:
    sample_lot_id: str
    parent_order_id: str
    part_id: str
    machine_id: str
    recipe_revision: str
    iteration: int
    qty: float
    uom: str
    disposition: str
    material_operation_ids: tuple[str, ...] = ()
    material_accounting: str = "unquantified"


@dataclass(frozen=True, slots=True)
class ProductionRun:
    expanded_problem: ProductionProblem
    schedule: ProductionSchedule
    verification: ProductionVerification
    qualification_runs: tuple[QualificationRun, ...]
    external_receipts: tuple[ExternalReceiptRun, ...]
    material_reservations: Mapping[str, MaterialReservation]
    quantity_balances: Mapping[str, LotBalance]
    unresolved_obligations: tuple[str, ...] = ()
    qualification_samples: tuple[QualificationSampleAccount, ...] = ()
    requested_problem: ProductionProblem | None = None
    completed: bool = True
    pending_operation_ids: tuple[str, ...] = ()
    unresolved_job_ids: tuple[str, ...] = ()
    customer_on_time_fraction: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "qualification_runs", tuple(self.qualification_runs))
        object.__setattr__(self, "external_receipts", tuple(self.external_receipts))
        object.__setattr__(self, "unresolved_obligations", tuple(self.unresolved_obligations))
        object.__setattr__(self, "qualification_samples", tuple(self.qualification_samples))
        object.__setattr__(self, "pending_operation_ids", tuple(self.pending_operation_ids))
        object.__setattr__(self, "unresolved_job_ids", tuple(self.unresolved_job_ids))
        object.__setattr__(
            self, "material_reservations", MappingProxyType(dict(self.material_reservations))
        )
        object.__setattr__(
            self, "quantity_balances", MappingProxyType(dict(self.quantity_balances))
        )


@dataclass(frozen=True, slots=True)
class _PendingQualificationRun:
    workflow_id: str
    trial: QualificationTrial
    sample_operation_id: str
    test_operation_ids: tuple[str, ...]
    adjustment_operation_id: str | None
    result: QualificationEvent


@dataclass(frozen=True, slots=True)
class _PendingReceipt:
    workflow: ExternalWorkflow
    plan: ExternalReceiptPlan
    ready_lot_id: str | None
    downstream_operation_id: str | None
    vendor_operation_id: str | None = None


def _expand_material_map(
    values: Mapping[str, tuple[_MaterialValue, ...]],
    operations: list[ProductionOperation],
    aliases: Mapping[str, str],
) -> Mapping[str, tuple[_MaterialValue, ...]]:
    """Apply exact operation keys, then explicit template-id suffixes."""

    expanded: dict[str, tuple[_MaterialValue, ...]] = {}
    for operation in operations:
        if operation.id in values:
            expanded[operation.id] = values[operation.id]
            continue
        alias = aliases.get(operation.id)
        if alias is not None and alias in values:
            expanded[operation.id] = values[alias]
            continue
        matches = sorted(key for key in values if operation.id.endswith(f":{key}"))
        if len(matches) > 1:
            raise ProductionRuntimeError(
                f"ambiguous material template keys for operation {operation.id!r}"
            )
        if matches:
            expanded[operation.id] = values[matches[0]]
    return MappingProxyType(expanded)


def _capture_state(ledger: object) -> dict[str, object]:
    """Capture mutable ledger containers without copying their thread locks."""

    state: dict[str, object] = {}
    for name, value in vars(ledger).items():
        if name == "_lock":
            continue
        if isinstance(value, dict):
            state[name] = dict(value)
        elif isinstance(value, list):
            state[name] = list(value)
        else:
            state[name] = value
    return state


def _restore_state(ledger: object, state: Mapping[str, object]) -> None:
    for name, value in state.items():
        setattr(ledger, name, value)


def _participating_ledgers(inputs: RuntimeInputs) -> tuple[object, ...]:
    objects: list[object] = [inputs.lot_ledger, inputs.material_ledger, inputs.external_ledger]
    objects.extend(item.state for item in inputs.qualification_workflows)
    unique: dict[int, object] = {id(item): item for item in objects}
    return tuple(unique.values())


@contextmanager
def _locked_ledgers(inputs: RuntimeInputs) -> Iterator[None]:
    """Hold every participating ledger lock in a stable order for one run."""

    ledgers = sorted(_participating_ledgers(inputs), key=id)
    with ExitStack() as stack:
        for ledger in ledgers:
            lock = getattr(ledger, "_lock", None)
            if lock is None:
                raise TypeError("runtime inputs must expose a private ledger lock")
            stack.enter_context(lock)
        yield


def _transaction_ledgers(inputs: RuntimeInputs) -> tuple[tuple[object, dict[str, object]], ...]:
    return tuple((ledger, _capture_state(ledger)) for ledger in _participating_ledgers(inputs))


def _rollback(snapshots: tuple[tuple[object, dict[str, object]], ...]) -> None:
    for ledger, state in snapshots:
        _restore_state(ledger, state)


def _sample_account(
    item: _PendingQualificationRun,
    inputs: RuntimeInputs,
    material_operation_ids: set[str],
) -> QualificationSampleAccount:
    workflow = next(
        workflow for workflow in inputs.qualification_workflows if workflow.id == item.workflow_id
    )
    trial_operation_ids = (
        item.sample_operation_id,
        *item.test_operation_ids,
        *((item.adjustment_operation_id,) if item.adjustment_operation_id is not None else ()),
    )
    material_ids = tuple(
        operation_id
        for operation_id in trial_operation_ids
        if operation_id in material_operation_ids
    )
    return QualificationSampleAccount(
        item.trial.sample_lot_id,
        workflow.sample_operation.order_id,
        workflow.state.plan.part_id,
        workflow.state.plan.machine_id,
        workflow.state.plan.recipe_revision,
        item.trial.iteration,
        workflow.state.plan.sample_qty,
        workflow.sample_uom,
        workflow.sample_disposition,
        material_ids,
        "quantified" if material_ids else "unquantified",
    )


def run(
    problem: ProductionProblem,
    inputs: RuntimeInputs,
    *,
    solver: str = "baseline",
) -> ProductionRun:
    """Compose one run while exclusively holding its participating ledgers."""

    with _locked_ledgers(inputs):
        return _run_locked(problem, inputs, solver=solver)


def _run_locked(
    problem: ProductionProblem,
    inputs: RuntimeInputs,
    *,
    solver: str = "baseline",
) -> ProductionRun:
    """Compose dynamic obligations, solve them, then apply attributed transitions."""

    snapshots = _transaction_ledgers(inputs)
    try:
        operations = list(_apply_active_wip(problem.operations, inputs.lot_ledger))
        _validate_lot_attribution(operations, inputs.lot_ledger)
        (
            operations,
            pending_qualifications,
            blocked_operation_ids,
            qualification_obligations,
        ) = _expand_qualifications(operations, inputs)
        operations = [item for item in operations if item.id not in blocked_operation_ids]
        (
            operations,
            pending_receipts,
            active_external_workflows,
            external_terminals,
            external_unresolved_sources,
            external_pending_operation_ids,
            external_obligations,
        ) = _expand_external(operations, inputs, blocked_operation_ids)
        material_aliases = _material_aliases(inputs, pending_qualifications, pending_receipts)
        _validate_material_keys(problem, inputs, operations, material_aliases)
        effective_inputs = replace(
            inputs,
            material_requirements=_expand_material_map(
                inputs.material_requirements, operations, material_aliases
            ),
            material_consumptions=_expand_material_map(
                inputs.material_consumptions, operations, material_aliases
            ),
        )
        operations = _apply_material_readiness(operations, inputs.material_ledger, effective_inputs)
        unresolved_job_ids = tuple(
            job.id
            for job in problem.jobs
            if any(
                operation_id in blocked_operation_ids or operation_id in external_unresolved_sources
                for operation_id in job.terminal_operation_ids
            )
        )
        unresolved_job_set = set(unresolved_job_ids)
        jobs = tuple(
            replace(
                job,
                terminal_operation_ids=tuple(
                    terminal
                    for operation_id in job.terminal_operation_ids
                    for terminal in external_terminals.get(operation_id, (operation_id,))
                ),
            )
            for job in problem.jobs
            if job.id not in unresolved_job_set
        )
        cohort = _executed_cohort(problem.cohort, jobs, unresolved_job_set)
        expanded = replace(problem, operations=tuple(operations), jobs=jobs, cohort=cohort)
        schedule = scheduling.solve(expanded, solver=solver)
        verification = scheduling.verify(expanded, schedule)
        if schedule.status not in {"FEASIBLE", "OPTIMAL"} or not verification.valid:
            _rollback(snapshots)
            return ProductionRun(
                expanded,
                schedule,
                verification,
                (),
                (),
                MappingProxyType({}),
                _balances(inputs.lot_ledger),
                ("schedule",),
                requested_problem=problem,
                completed=False,
                pending_operation_ids=tuple(sorted(blocked_operation_ids)),
                unresolved_job_ids=tuple(job.id for job in problem.jobs),
                customer_on_time_fraction=None,
            )
        assignments = {item.operation_id: item for item in schedule.assignments}
        _validate_qualification_assignments(expanded, inputs, assignments)
        material_reservations = _apply_materials(effective_inputs, assignments)
        external_receipts = _apply_external(
            inputs, pending_receipts, active_external_workflows, assignments
        )
        external_sources = {item.source_operation_id for item in active_external_workflows}
        external_sources.update(
            f"{item.operation.operation_id}:vendor"
            for item in active_external_workflows
            if not item.operation.unknown_capacity
        )
        _complete_terminal_lots(
            expanded, inputs.lot_ledger, schedule, excluded_operation_ids=external_sources
        )
        unresolved = (*qualification_obligations, *external_obligations)
        pending_operation_ids = tuple(
            dict.fromkeys((*sorted(blocked_operation_ids), *external_pending_operation_ids))
        )
        unresolved_customer = any(
            job.id in unresolved_job_set and job.demand_kind == "customer" for job in problem.jobs
        )
        return ProductionRun(
            expanded,
            schedule,
            verification,
            tuple(_public_qualification(item) for item in pending_qualifications),
            tuple(external_receipts),
            MappingProxyType(material_reservations),
            _balances(inputs.lot_ledger),
            tuple(unresolved),
            tuple(
                _sample_account(item, inputs, set(material_reservations))
                for item in pending_qualifications
            ),
            problem,
            not unresolved,
            pending_operation_ids,
            unresolved_job_ids,
            None if unresolved_customer else schedule.customer_on_time_fraction,
        )
    except Exception:
        _rollback(snapshots)
        raise


def _material_aliases(
    inputs: RuntimeInputs,
    qualifications: list[_PendingQualificationRun],
    receipts: list[_PendingReceipt],
) -> Mapping[str, str]:
    workflows = {item.id: item for item in inputs.qualification_workflows}
    aliases: dict[str, str] = {}
    for item in qualifications:
        workflow = workflows[item.workflow_id]
        aliases[item.sample_operation_id] = workflow.sample_operation.id
        aliases.update(
            {
                operation_id: template.id
                for operation_id, template in zip(
                    item.test_operation_ids, workflow.test_operations, strict=True
                )
            }
        )
        if item.adjustment_operation_id is not None:
            aliases[item.adjustment_operation_id] = workflow.adjustment_operation.id
    for receipt in receipts:
        if receipt.downstream_operation_id is not None:
            aliases[receipt.downstream_operation_id] = receipt.workflow.downstream_operation.id
        if (
            receipt.vendor_operation_id is not None
            and receipt.workflow.vendor_operation is not None
        ):
            aliases[receipt.vendor_operation_id] = receipt.workflow.vendor_operation.id
    return MappingProxyType(aliases)


def _validate_material_keys(
    problem: ProductionProblem,
    inputs: RuntimeInputs,
    operations: list[ProductionOperation],
    aliases: Mapping[str, str],
) -> None:
    requirement_keys = set(inputs.material_requirements)
    consumption_keys = set(inputs.material_consumptions)
    if requirement_keys != consumption_keys:
        raise ProductionRuntimeError(
            "material requirements and consumptions must cover the same operations"
        )
    known = {item.id for item in problem.operations} | {item.id for item in operations}
    known.update(aliases.values())
    for qualification_workflow in inputs.qualification_workflows:
        known.add(qualification_workflow.sample_operation.id)
        known.add(qualification_workflow.adjustment_operation.id)
        known.update(item.id for item in qualification_workflow.test_operations)
    for external_workflow in inputs.external_workflows:
        known.add(external_workflow.downstream_operation.id)
        if external_workflow.vendor_operation is not None:
            known.add(external_workflow.vendor_operation.id)
    unknown = requirement_keys - known
    if unknown:
        raise ProductionRuntimeError(f"unknown material operation(s): {sorted(unknown)!r}")


def _validate_lot_attribution(operations: list[ProductionOperation], ledger: LotLedger) -> None:
    """Reject cross-order lot references before any state is advanced."""

    for operation in operations:
        for lot_id in operation.lot_ids:
            try:
                lot = ledger.get(lot_id)
            except KeyError as error:
                raise ProductionRuntimeError(
                    f"operation {operation.id!r} references unknown lot {lot_id!r}"
                ) from error
            if lot.order_id != operation.order_id:
                raise ProductionRuntimeError(
                    f"operation {operation.id!r} references a lot from another order"
                )


def _executed_cohort(
    cohort: ScenarioCohort | None,
    jobs: tuple[ProductionJob, ...],
    unresolved_job_ids: set[str],
) -> ScenarioCohort | None:
    if cohort is None or not unresolved_job_ids:
        return cohort
    executed_ids = {job.id for job in jobs}
    return replace(
        cohort,
        id=f"{cohort.id}:executed",
        included_job_ids=tuple(
            job_id for job_id in cohort.included_job_ids if job_id in executed_ids
        ),
    )


def _apply_active_wip(
    operations: tuple[ProductionOperation, ...], ledger: LotLedger
) -> tuple[ProductionOperation, ...]:
    result: list[ProductionOperation] = []
    for operation in operations:
        active = [
            ledger.get(lot_id)
            for lot_id in operation.lot_ids
            if lot_id in ledger.lots and ledger.get(lot_id).state == "active"
        ]
        if not active:
            result.append(operation)
            continue
        if len(active) != 1 or len(operation.alternatives) != 1:
            raise ProductionRuntimeError("active WIP requires one lot and one retained recipe")
        lot = active[0]
        alternative = operation.alternatives[0]
        if lot.machine_id is not None and lot.machine_id != alternative.primary_resource_id:
            raise ProductionRuntimeError("active WIP machine differs from retained machine")
        if lot.setup_state is not None and lot.setup_state != alternative.revision:
            raise ProductionRuntimeError("active WIP setup state differs from retained revision")
        if lot.remaining_time <= 0 or not alternative.phases:
            result.append(operation)
            continue
        phases = tuple(phase for phase in alternative.phases if phase.id.lower() != "setup")
        if not phases:
            raise ProductionRuntimeError("active WIP recipe has no resumable production phase")
        run_phase = replace(phases[-1], duration=lot.remaining_time)
        first_phase_id = phases[0].id
        uses = tuple(
            replace(
                use,
                start_phase=(
                    first_phase_id if use.start_phase.lower() == "setup" else use.start_phase
                ),
                end_phase=first_phase_id if use.end_phase.lower() == "setup" else use.end_phase,
            )
            for use in alternative.uses
            if use.start_phase.lower() != "setup" or use.end_phase.lower() != "setup"
        )
        updated_alternative = replace(alternative, phases=(*phases[:-1], run_phase), uses=uses)
        result.append(replace(operation, alternatives=(updated_alternative,)))
    return tuple(result)


def _apply_material_readiness(
    operations: list[ProductionOperation],
    ledger: MaterialLedger,
    inputs: RuntimeInputs,
) -> list[ProductionOperation]:
    updated: list[ProductionOperation] = []
    for operation in operations:
        requirements = inputs.material_requirements.get(operation.id)
        if not requirements:
            updated.append(operation)
            continue
        try:
            ready_at = ledger.earliest_ready_time(requirements)
        except Exception as error:
            raise ProductionRuntimeError(
                f"material readiness cannot be established for {operation.id!r}"
            ) from error
        updated.append(replace(operation, release_time=max(operation.release_time, ready_at)))
    return updated


def _expand_qualifications(
    operations: list[ProductionOperation], inputs: RuntimeInputs
) -> tuple[
    list[ProductionOperation],
    list[_PendingQualificationRun],
    set[str],
    list[str],
]:
    by_id = {item.id: item for item in operations}
    workflow_ids = {workflow.id for workflow in inputs.qualification_workflows}
    unknown = {
        item.qualification_id
        for item in operations
        if item.qualification_id is not None and item.qualification_id not in workflow_ids
    }
    if unknown:
        raise ProductionRuntimeError(f"unknown qualification workflow(s): {sorted(unknown)!r}")
    pending: list[_PendingQualificationRun] = []
    blocked: set[str] = set()
    unresolved: list[str] = []
    for workflow in inputs.qualification_workflows:
        gated = [item for item in operations if item.qualification_id == workflow.id]
        if len(gated) != 1:
            raise ProductionRuntimeError(
                f"qualification {workflow.id!r} must gate exactly one operation"
            )
        main = gated[0]
        qualified_alternatives = _validate_qualification_workflow(main, workflow)
        main = replace(main, alternatives=qualified_alternatives)
        by_id[main.id] = main
        if workflow.state.ready_for_production:
            continue
        predecessor_ids = main.predecessors
        approved_test_id: str | None = None
        for index, attempt in enumerate(workflow.attempts, start=1):
            trial = workflow.state.start_trial()
            prefix = f"{workflow.id}:trial:{index}"
            sample = _clone_operation(
                workflow.sample_operation,
                f"{prefix}:sample",
                predecessors=predecessor_ids,
                release_time=max(workflow.sample_operation.release_time, main.release_time),
                lot_ids=(trial.sample_lot_id,),
            )
            operations.append(sample)
            previous = sample.id
            test_ids: list[str] = []
            for template in workflow.test_operations:
                test = _clone_operation(
                    template,
                    f"{prefix}:test:{template.id}",
                    predecessors=(previous,),
                    lot_ids=(trial.sample_lot_id,),
                )
                operations.append(test)
                test_ids.append(test.id)
                previous = test.id
            event = workflow.state.record_result(
                trial.sample_lot_id, attempt.passed, attempt.measurements
            )
            adjustment_id: str | None = None
            if event.kind == "adjustment":
                adjustment = _clone_operation(
                    workflow.adjustment_operation,
                    f"{prefix}:adjustment",
                    predecessors=(previous,),
                )
                operations.append(adjustment)
                adjustment_id = adjustment.id
                predecessor_ids = (adjustment.id,)
            elif event.kind == "approved":
                approved_test_id = previous
            pending.append(
                _PendingQualificationRun(
                    workflow.id,
                    trial,
                    sample.id,
                    tuple(test_ids),
                    adjustment_id,
                    event,
                )
            )
            if event.kind == "approved":
                break
            if event.kind == "unresolved":
                break
        if not workflow.state.ready_for_production or approved_test_id is None:
            closure = _descendant_closure(main.id, operations)
            blocked.update(closure)
            unresolved.append(f"qualification:{workflow.id}:unresolved")
            continue
        by_id[main.id] = replace(main, predecessors=(*main.predecessors, approved_test_id))
    return [by_id.get(item.id, item) for item in operations], pending, blocked, unresolved


def _descendant_closure(operation_id: str, operations: list[ProductionOperation]) -> set[str]:
    blocked = {operation_id}
    changed = True
    while changed:
        changed = False
        for operation in operations:
            if operation.id not in blocked and any(
                predecessor in blocked for predecessor in operation.predecessors
            ):
                blocked.add(operation.id)
                changed = True
    return blocked


def _validate_qualification_workflow(
    main: ProductionOperation, workflow: QualificationWorkflow
) -> tuple[RecipeAlternative, ...]:
    plan = workflow.state.plan
    if tuple(item.id for item in workflow.test_operations) != plan.test_route:
        raise ProductionRuntimeError("qualification test route differs from its plan")
    matches = [
        alternative
        for alternative in main.alternatives
        if alternative.primary_resource_id == plan.machine_id
        and alternative.revision == plan.recipe_revision
    ]
    if not matches:
        raise ProductionRuntimeError("qualification machine/revision differs from gated recipe")
    return tuple(matches)


def _validate_qualification_assignments(
    problem: ProductionProblem,
    inputs: RuntimeInputs,
    assignments: Mapping[str, OperationAssignment],
) -> None:
    workflows = {workflow.id: workflow for workflow in inputs.qualification_workflows}
    for operation in problem.operations:
        if operation.qualification_id is None:
            continue
        workflow = workflows[operation.qualification_id]
        assignment = assignments[operation.id]
        alternative = next(
            item for item in operation.alternatives if item.id == assignment.alternative_id
        )
        plan = workflow.state.plan
        if (
            alternative.primary_resource_id != plan.machine_id
            or alternative.revision != plan.recipe_revision
        ):
            raise ProductionRuntimeError(
                "scheduled qualification recipe differs from approved recipe"
            )


def _expand_external(
    operations: list[ProductionOperation],
    inputs: RuntimeInputs,
    blocked_operation_ids: set[str],
) -> tuple[
    list[ProductionOperation],
    list[_PendingReceipt],
    tuple[ExternalWorkflow, ...],
    Mapping[str, tuple[str, ...]],
    set[str],
    tuple[str, ...],
    tuple[str, ...],
]:
    known = {item.id for item in operations}
    requested_ids = known | blocked_operation_ids
    pending: list[_PendingReceipt] = []
    active: list[ExternalWorkflow] = []
    terminals: dict[str, tuple[str, ...]] = {}
    unresolved_sources: set[str] = set()
    pending_operation_ids: list[str] = []
    unresolved: list[str] = []
    for workflow in inputs.external_workflows:
        if workflow.source_operation_id not in requested_ids:
            raise ProductionRuntimeError("external source operation is unknown")
        lot = inputs.lot_ledger.get(workflow.lot_id)
        if not math.isclose(lot.qty, workflow.qty, rel_tol=0.0, abs_tol=1e-9):
            raise ProductionRuntimeError("external dispatch quantity differs from its lot")
        if lot.order_id != workflow.operation.order_id:
            raise ProductionRuntimeError("external lot belongs to another order")
        if lot.uom != workflow.uom:
            raise ProductionRuntimeError("external lot unit differs from its workflow")
        if workflow.downstream_operation.order_id != lot.order_id:
            raise ProductionRuntimeError("external downstream operation belongs to another order")
        if workflow.source_operation_id in blocked_operation_ids:
            unresolved_sources.add(workflow.source_operation_id)
            pending_operation_ids.append(workflow.downstream_operation.id)
            unresolved.append(
                f"external:{workflow.operation.operation_id}:source_operation_pending"
            )
            continue
        active.append(workflow)
        vendor_id: str | None = None
        if workflow.operation.unknown_capacity:
            vendor_predecessor = workflow.source_operation_id
        else:
            if workflow.vendor_operation is None:
                raise ProductionRuntimeError(
                    "known-capacity external processing requires a vendor operation"
                )
            if workflow.vendor_operation.order_id != lot.order_id:
                raise ProductionRuntimeError("external vendor operation belongs to another order")
            vendor_id = f"{workflow.operation.operation_id}:vendor"
            if vendor_id in known:
                raise ProductionRuntimeError("duplicate external vendor operation")
            vendor = _clone_operation(
                workflow.vendor_operation,
                vendor_id,
                predecessors=(workflow.source_operation_id,),
                lot_ids=(workflow.lot_id,),
            )
            operations.append(vendor)
            known.add(vendor_id)
            vendor_predecessor = vendor_id
        generated: list[str] = []
        remaining = workflow.qty
        accepted_total = 0.0
        for index, receipt in enumerate(workflow.receipts, start=1):
            accounted = receipt.accepted_qty + receipt.rejected_qty + receipt.lost_qty
            if accounted > remaining + 1e-9:
                raise ProductionRuntimeError("external receipt exceeds remaining attributed lot")
            ready_lot_id: str | None = None
            downstream_id: str | None = None
            if receipt.accepted_qty > 0:
                source_after_loss = remaining - receipt.rejected_qty - receipt.lost_qty
                ready_lot_id = (
                    workflow.lot_id
                    if math.isclose(
                        receipt.accepted_qty,
                        source_after_loss,
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    else f"{workflow.lot_id}:receipt:{index}"
                )
                downstream_id = f"{workflow.downstream_operation.id}:receipt:{index}"
                downstream = _clone_operation(
                    workflow.downstream_operation,
                    downstream_id,
                    predecessors=(vendor_predecessor,),
                    release_time=max(workflow.downstream_operation.release_time, receipt.at),
                    lot_ids=(ready_lot_id,),
                )
                operations.append(downstream)
                generated.append(downstream_id)
            pending.append(
                _PendingReceipt(workflow, receipt, ready_lot_id, downstream_id, vendor_id)
            )
            remaining -= accounted
            accepted_total += receipt.accepted_qty
        if generated:
            terminals[workflow.source_operation_id] = tuple(generated)
        if remaining > 1e-9:
            unresolved_sources.add(workflow.source_operation_id)
            pending_operation_ids.append(workflow.downstream_operation.id)
            unresolved.append(f"external:{workflow.operation.operation_id}:vendor_quantity_pending")
        elif accepted_total < workflow.qty - 1e-9:
            unresolved_sources.add(workflow.source_operation_id)
            unresolved.append(
                f"external:{workflow.operation.operation_id}:customer_quantity_shortfall"
            )
    return (
        operations,
        pending,
        tuple(active),
        terminals,
        unresolved_sources,
        tuple(pending_operation_ids),
        tuple(unresolved),
    )


def _clone_operation(
    template: ProductionOperation,
    operation_id: str,
    *,
    predecessors: tuple[str, ...],
    release_time: float | None = None,
    lot_ids: tuple[str, ...] | None = None,
) -> ProductionOperation:
    return replace(
        template,
        id=operation_id,
        predecessors=predecessors,
        release_time=template.release_time if release_time is None else release_time,
        lot_ids=template.lot_ids if lot_ids is None else lot_ids,
        qualification_id=None,
    )


def _apply_materials(
    inputs: RuntimeInputs, assignments: Mapping[str, OperationAssignment]
) -> dict[str, MaterialReservation]:
    reservations: dict[str, MaterialReservation] = {}
    if set(inputs.material_requirements) != set(inputs.material_consumptions):
        raise ProductionRuntimeError(
            "material requirements and consumptions must cover the same operations"
        )
    for operation_id, requirements in inputs.material_requirements.items():
        assignment = assignments.get(operation_id)
        if assignment is None:
            raise ProductionRuntimeError(f"material operation {operation_id!r} was not scheduled")
        at = float(assignment.start)
        reservation = inputs.material_ledger.reserve(
            requirements, at=at, reservation_id=f"runtime:{operation_id}"
        )
        entries = inputs.material_consumptions.get(operation_id)
        if entries is None:
            inputs.material_ledger.release(reservation)
            raise ProductionRuntimeError(f"material consumption missing for {operation_id!r}")
        inputs.material_ledger.consume(
            reservation, entries=entries, event_id=f"runtime:{operation_id}:consume"
        )
        reservations[operation_id] = reservation
    return reservations


def _apply_external(
    inputs: RuntimeInputs,
    pending: list[_PendingReceipt],
    active_workflows: tuple[ExternalWorkflow, ...],
    assignments: Mapping[str, OperationAssignment],
) -> list[ExternalReceiptRun]:
    result: list[ExternalReceiptRun] = []
    dispatched: dict[int, str] = {}
    remaining_by_flow: dict[int, float] = {}
    for workflow in active_workflows:
        key = id(workflow)
        inputs.external_ledger.register(workflow.operation)
        source = assignments.get(workflow.source_operation_id)
        if source is None:
            raise ProductionRuntimeError("external source operation was not scheduled")
        outbound = inputs.external_ledger.dispatch(
            workflow.operation.operation_id,
            workflow.lot_id,
            workflow.qty,
            workflow.uom,
            at=float(source.end),
            event_id=f"runtime:{workflow.operation.operation_id}:dispatch",
        )
        dispatched[key] = outbound.shipment_id
        remaining_by_flow[key] = workflow.qty
        inputs.lot_ledger.transfer(workflow.lot_id, workflow.operation.operation_id, "wip")
    for item in pending:
        key = id(item.workflow)
        plan = item.plan
        if item.vendor_operation_id is not None:
            vendor_assignment = assignments[item.vendor_operation_id]
            if plan.at < vendor_assignment.end - 1e-9:
                raise ProductionRuntimeError(
                    "external receipt precedes the scheduled vendor operation"
                )
        receipt = inputs.external_ledger.receive(
            item.workflow.operation.operation_id,
            dispatched[key],
            plan.accepted_qty,
            plan.rejected_qty,
            plan.lost_qty,
            plan.at,
            plan.event_id,
        )
        remaining = remaining_by_flow[key]
        accounted = plan.accepted_qty + plan.rejected_qty + plan.lost_qty
        if accounted > remaining + 1e-9:
            raise ProductionRuntimeError("external receipt exceeds remaining attributed lot")
        loss = plan.rejected_qty + plan.lost_qty
        if loss > 0:
            inputs.lot_ledger.scrap(
                item.workflow.lot_id,
                loss,
                "external processing rejected or lost",
            )
        if plan.accepted_qty <= 0:
            remaining_by_flow[key] -= accounted
            result.append(ExternalReceiptRun(receipt, None, ""))
            continue
        source_qty = inputs.lot_ledger.get(item.workflow.lot_id).qty
        if plan.accepted_qty < source_qty - 1e-9:
            if item.ready_lot_id is None:
                raise ProductionRuntimeError("accepted external receipt has no ready lot")
            inputs.lot_ledger.split(item.workflow.lot_id, plan.accepted_qty, item.ready_lot_id)
        elif abs(plan.accepted_qty - source_qty) > 1e-9:
            raise ProductionRuntimeError("accepted receipt exceeds remaining attributed lot")
        elif item.ready_lot_id != item.workflow.lot_id:
            raise ProductionRuntimeError(
                "full external receipt must retain its source lot identity"
            )
        inputs.lot_ledger.transfer(
            item.ready_lot_id, item.workflow.downstream_operation.id, "queued"
        )
        remaining_by_flow[key] -= accounted
        result.append(ExternalReceiptRun(receipt, item.ready_lot_id, item.downstream_operation_id))
    return result


def _complete_terminal_lots(
    problem: ProductionProblem,
    ledger: LotLedger,
    schedule: ProductionSchedule,
    *,
    excluded_operation_ids: set[str] | None = None,
) -> None:
    parents = {parent for operation in problem.operations for parent in operation.predecessors}
    excluded = excluded_operation_ids or set()
    for assignment in schedule.assignments:
        if assignment.operation_id in parents or assignment.operation_id in excluded:
            continue
        for lot_id in assignment.lot_ids:
            if lot_id not in ledger.lots:
                continue
            lot = ledger.get(lot_id)
            if lot.state in {"accepted", "scrap"} or lot.qty <= 0:
                continue
            # Keep the accepted quantity on the identified lot.  ``accept`` is
            # for consuming a quantity from a larger lot; terminal completion
            # changes state while retaining the lot's measured quantity.
            terminal_operation_id = assignment.operation_id.split(":receipt:", 1)[0]
            ledger.transfer(lot_id, terminal_operation_id, "accepted")


def _balances(ledger: LotLedger) -> Mapping[str, LotBalance]:
    order_ids = {lot.order_id for lot in ledger.lots.values()}
    return MappingProxyType({order_id: ledger.balance(order_id) for order_id in order_ids})


def _public_qualification(item: _PendingQualificationRun) -> QualificationRun:
    return QualificationRun(
        item.workflow_id,
        item.trial,
        item.sample_operation_id,
        item.test_operation_ids,
        item.adjustment_operation_id,
        item.result,
    )
