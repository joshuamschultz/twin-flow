"""Typed value objects for integrated production scheduling."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Status = Literal["FEASIBLE", "OPTIMAL", "INFEASIBLE", "UNKNOWN"]
SegmentKind = Literal["work", "restart"]
OccupationKind = Literal["productive", "hold", "restart"]


@dataclass(frozen=True, slots=True)
class TimeWindow:
    start: float
    end: float


@dataclass(frozen=True, slots=True)
class Resource:
    id: str
    kind: str
    capacity: int = 1
    windows: tuple[TimeWindow, ...] = ()
    qualifications: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "windows", tuple(self.windows))
        object.__setattr__(self, "qualifications", frozenset(self.qualifications))


@dataclass(frozen=True, slots=True)
class RestartRule:
    idle_threshold: float
    duration: float
    resource_ids: tuple[str, ...] = ()
    quantity: int = 1
    qualifications: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_ids", tuple(self.resource_ids))
        object.__setattr__(self, "qualifications", frozenset(self.qualifications))


@dataclass(frozen=True, slots=True)
class Phase:
    id: str
    duration: float
    interruptible: bool = False
    restart_rule: RestartRule | None = None


@dataclass(frozen=True, slots=True)
class ResourceUse:
    resource_ids: tuple[str, ...]
    start_phase: str
    end_phase: str
    quantity: int = 1
    qualifications: frozenset[str] = frozenset()
    hold_during_pause: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_ids", tuple(self.resource_ids))
        object.__setattr__(self, "qualifications", frozenset(self.qualifications))


@dataclass(frozen=True, slots=True)
class ThermalRecipe:
    id: str
    revision: str
    capacity: float
    capacity_uom: str
    warmup_duration: float
    hold_duration: float
    cooldown_duration: float = 0.0
    compatibility_key: str = ""


@dataclass(frozen=True, slots=True)
class BatchRequirement:
    lot_id: str
    quantity: float
    uom: str
    thermal_recipe_id: str


@dataclass(frozen=True, slots=True)
class RecipeAlternative:
    id: str
    primary_resource_id: str
    revision: str
    phases: tuple[Phase, ...]
    uses: tuple[ResourceUse, ...]
    preference_cost: float = 0.0
    batch: BatchRequirement | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(self.phases))
        object.__setattr__(self, "uses", tuple(self.uses))


@dataclass(frozen=True, slots=True)
class ProductionOperation:
    id: str
    order_id: str
    alternatives: tuple[RecipeAlternative, ...]
    predecessors: tuple[str, ...] = ()
    release_time: float = 0.0
    lot_ids: tuple[str, ...] = ()
    qualification_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "alternatives", tuple(self.alternatives))
        object.__setattr__(self, "predecessors", tuple(self.predecessors))
        object.__setattr__(self, "lot_ids", tuple(self.lot_ids))


@dataclass(frozen=True, slots=True)
class ProductionJob:
    id: str
    terminal_operation_ids: tuple[str, ...]
    demand_kind: str = "production"
    promised_ship_time: float | None = None
    priority: int = 1
    sales_order_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminal_operation_ids", tuple(self.terminal_operation_ids))
        object.__setattr__(self, "sales_order_refs", tuple(self.sales_order_refs))


@dataclass(frozen=True, slots=True)
class ScenarioCohort:
    id: str
    snapshot_at: str
    included_job_ids: tuple[str, ...]
    total_work_order_count: int
    pending_demand_count: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "included_job_ids", tuple(self.included_job_ids))


@dataclass(frozen=True, slots=True)
class ProductionProblem:
    resources: tuple[Resource, ...]
    operations: tuple[ProductionOperation, ...]
    jobs: tuple[ProductionJob, ...] = ()
    thermal_recipes: tuple[ThermalRecipe, ...] = ()
    time_unit: str = "minutes"
    objective: str = "makespan"
    cohort: ScenarioCohort | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "resources", tuple(self.resources))
        object.__setattr__(self, "operations", tuple(self.operations))
        object.__setattr__(self, "jobs", tuple(self.jobs))
        object.__setattr__(self, "thermal_recipes", tuple(self.thermal_recipes))


@dataclass(frozen=True, slots=True)
class ProductionIssue:
    code: str
    path: str
    message: str


@dataclass(frozen=True, slots=True)
class PhaseSegment:
    operation_id: str
    phase_id: str
    start: float
    end: float
    kind: SegmentKind = "work"


@dataclass(frozen=True, slots=True)
class ResourceOccupation:
    operation_id: str
    resource_id: str
    start: float
    end: float
    quantity: int = 1
    kind: OccupationKind = "productive"


@dataclass(frozen=True, slots=True)
class OperationAssignment:
    operation_id: str
    order_id: str
    alternative_id: str
    recipe_revision: str
    start: float
    end: float
    segments: tuple[PhaseSegment, ...] = ()
    occupations: tuple[ResourceOccupation, ...] = ()
    lot_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "segments", tuple(self.segments))
        object.__setattr__(self, "occupations", tuple(self.occupations))
        object.__setattr__(self, "lot_ids", tuple(self.lot_ids))


@dataclass(frozen=True, slots=True)
class BatchAssignment:
    id: str
    recipe_id: str
    resource_id: str
    member_operation_ids: tuple[str, ...]
    total_quantity: float
    uom: str
    start: float
    end: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "member_operation_ids", tuple(self.member_operation_ids))


@dataclass(frozen=True, slots=True)
class JobResult:
    job_id: str
    completion_time: float
    tardiness: float | None
    on_time: bool | None
    customer_demand: bool


@dataclass(frozen=True, slots=True)
class ProductionSchedule:
    status: Status
    assignments: tuple[OperationAssignment, ...] = ()
    batches: tuple[BatchAssignment, ...] = ()
    job_results: tuple[JobResult, ...] = ()
    objective_value: float | None = None
    customer_on_time_fraction: float | None = None
    cohort: ScenarioCohort | None = None
    verified: bool = False
    issues: tuple[ProductionIssue, ...] = ()
    solver: str = "baseline"

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignments", tuple(self.assignments))
        object.__setattr__(self, "batches", tuple(self.batches))
        object.__setattr__(self, "job_results", tuple(self.job_results))
        object.__setattr__(self, "issues", tuple(self.issues))


@dataclass(frozen=True, slots=True)
class ProductionVerification:
    valid: bool
    issues: tuple[ProductionIssue, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleComparison:
    comparable: bool
    issues: tuple[ProductionIssue, ...] = ()
    objective_delta: float | None = None


def frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    """Snapshot a caller-owned mapping at a public contract boundary."""

    return MappingProxyType(dict(value))
