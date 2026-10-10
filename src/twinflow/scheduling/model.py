"""Problem model v2: the typed, immutable contract of the scheduling package (COMP-001).

Every type is a frozen Pydantic v2 model that forbids unknown fields and rejects
non-finite numbers (D-060). Collections are tuples so a validated value cannot be
changed in place. Ids are 1 to 200 characters, free text at most 2,000, and the input
is size-bounded (REQ-084). This module validates fields only; scheduling meaning
(references, cycles, objectives, overrides) lives in the modules that read these types.
Times are float seconds.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

SCHEMA_VERSION = "2.0"

MAX_OPERATIONS = 100_000
MAX_MACHINES = 10_000
MAX_CALENDAR_INTERVALS = 1_000_000
MAX_OVERRIDES = 10_000
MAX_ID_LENGTH = 200
MAX_TEXT_LENGTH = 2_000
WEIGHT_CLASSES = frozenset({1, 2, 4})
SPLIT_SUM_TOLERANCE = 1e-9

Id = Annotated[str, Field(min_length=1, max_length=MAX_ID_LENGTH)]
Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_LENGTH)]
NonNegFloat = Annotated[float, Field(ge=0)]
PosFloat = Annotated[float, Field(gt=0)]
LoadTarget = Annotated[float, Field(gt=0, le=1)]
WeightClass = Literal[1, 2, 4]
SetupValue = float | Annotated[str, Field(max_length=MAX_TEXT_LENGTH)]
SetupParams = dict[Id, SetupValue]
Interval = tuple[float, float]


class _Contract(BaseModel):
    """Shared config: immutable, closed to unknown fields, finite floats only (D-060)."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ObjectiveKind(StrEnum):
    """The thirteen schedule objectives (REQ-068); formulas live in objectives.py."""

    MAKESPAN = "makespan"
    WEIGHTED_COMPLETION = "weighted_completion"
    MAX_LATENESS = "max_lateness"
    TARDY_COUNT = "tardy_count"
    WEIGHTED_TARDY_COUNT = "weighted_tardy_count"
    TOTAL_TARDINESS = "total_tardiness"
    WEIGHTED_TARDINESS = "weighted_tardiness"
    EARLINESS_TARDINESS = "earliness_tardiness"
    TOTAL_SETUP = "total_setup"
    COST = "cost"
    CHURN = "churn"
    ROBUSTNESS = "robustness"
    SOFT_PENALTY = "soft_penalty"


# --- operations and jobs ------------------------------------------------------------------


class ProcessingOption(_Contract):
    """One eligible machine for an operation; run seconds = fixed + quantity * unit (D-061)."""

    machine_id: Id
    fixed_seconds: NonNegFloat
    unit_seconds: NonNegFloat
    preference_cost: NonNegFloat


class Precedence(_Contract):
    """End-to-gate link from the predecessor `operation_id`, with lags and optional overlap.

    `overlap_quantity` is a transfer lot: the successor may start once that much of the
    predecessor's lot is done (D-061).
    """

    operation_id: Id
    min_lag: NonNegFloat = 0.0
    max_lag: float | None = None
    overlap_quantity: PosFloat | None = None

    @model_validator(mode="after")
    def _max_lag_not_below_min_lag(self) -> Self:
        if self.max_lag is not None and self.max_lag < self.min_lag:
            raise ValueError("max_lag must be >= min_lag")
        return self


class OperatorNeed(_Contract):
    """Operators from a pool, held during the setup phase, the run phase or both (D-063)."""

    pool_id: Id
    quantity: int = Field(ge=1)
    phase: Literal["setup", "run", "both"]


class Pin(_Contract):
    """Fixes an operation's machine and/or a window for its start."""

    machine_id: Id | None = None
    earliest_start: float | None = None
    latest_start: float | None = None


class OperationStatus(_Contract):
    """Shop-floor state: in-progress operations are placed at `now`; done ones are skipped."""

    state: Literal["pending", "in_progress", "done"] = "pending"
    machine_id: Id | None = None
    remaining_seconds: float | None = None


class Operation(_Contract):
    """One processing step of a whole lot of `quantity` units, never split into pieces (D-061).

    Setup is the first phase of the operation, not a separate task (D-063): the machine is
    busy on `[start, end)` and `setup_detached` chooses whether material must be present
    from `start` (false) or only from `setup_end` (true). `shift_crossing` says whether
    the operation must fit inside one working interval or may pause over breaks (D-065).
    """

    id: Id
    work_center_id: Id
    quantity: PosFloat
    options: tuple[ProcessingOption, ...] = Field(min_length=1)
    predecessors: tuple[Precedence, ...] = ()
    ready_time: float = 0.0
    setup_params: SetupParams = Field(default_factory=dict)
    setup_family: Id | None = None
    batch_family: Id | None = None
    setup_detached: StrictBool = False
    operator_needs: tuple[OperatorNeed, ...] = ()
    shift_crossing: Literal["forbid", "pause"] = "forbid"
    pin: Pin | None = None
    status: OperationStatus = OperationStatus()


class Job(_Contract):
    """A customer order made of operations; its effective weight comes from its class."""

    id: Id
    customer: Text | None = None
    weight_class: WeightClass = 1
    weight_override: PosFloat | None = None
    release: float = 0.0
    due: float | None = None
    promised_ship: float | None = None
    status: Literal["open", "held", "done"] = "open"
    sales_order_refs: tuple[Id, ...] = ()
    operations: tuple[Operation, ...] = Field(max_length=MAX_OPERATIONS)


# --- resources ------------------------------------------------------------------------------


class MachineState(_Contract):
    """What the machine is doing at `now`: when it frees up and how it is set up."""

    available_from: float = 0.0
    last_setup_params: SetupParams = Field(default_factory=dict)
    last_setup_family: Id | None = None


class BatchCapacity(_Contract):
    """Batch limit of a machine, in a named unit (scheduled from TF-003)."""

    capacity: PosFloat
    unit: Id


class RestartRule(_Contract):
    """Warm-up needed after the machine has idled longer than the threshold (D-063)."""

    idle_threshold_seconds: PosFloat
    warmup_seconds: PosFloat


class Machine(_Contract):
    """A unary machine: one operation at a time; more capacity is more machines (D-062).

    `calendar_id` None means always working (D-065).
    """

    id: Id
    work_center_id: Id
    calendar_id: Id | None = None
    setup_function_id: Id | None = None
    batch: BatchCapacity | None = None
    restart: RestartRule | None = None
    hour_cost: NonNegFloat = 0.0
    setup_hour_cost: NonNegFloat = 0.0
    state: MachineState = MachineState()


class WorkCenter(_Contract):
    """A group of machines; its setup function applies when a machine has none (D-064)."""

    id: Id
    setup_function_id: Id | None = None


class Calendar(_Contract):
    """Explicit working intervals `[start, end)`, sorted and non-overlapping (D-065)."""

    id: Id
    intervals: tuple[Interval, ...] = Field(max_length=MAX_CALENDAR_INTERVALS)

    @model_validator(mode="after")
    def _intervals_sorted_and_disjoint(self) -> Self:
        previous_end = -math.inf
        for start, end in self.intervals:
            if start >= end or start < previous_end:
                raise ValueError("calendar intervals must be sorted, disjoint and start < end")
            previous_end = end
        return self


class OperatorPool(_Contract):
    """Interchangeable operators with an optional shift calendar (D-063, D-065)."""

    id: Id
    size: int = Field(ge=1)
    calendar_id: Id | None = None


# --- setups ---------------------------------------------------------------------------------


class AbsDiffTerm(_Contract):
    """Setup term: `seconds_per_unit * |a - b|` over a numeric setup parameter (D-064)."""

    kind: Literal["abs_diff"] = "abs_diff"
    param: Id
    seconds_per_unit: NonNegFloat


class MismatchTerm(_Contract):
    """Setup term: `seconds` when a setup parameter differs, else 0 (D-064)."""

    kind: Literal["mismatch"] = "mismatch"
    param: Id
    seconds: NonNegFloat


SetupTerm = Annotated[AbsDiffTerm | MismatchTerm, Field(discriminator="kind")]


class FamilyMatrixEntry(_Contract):
    """Changeover seconds from one setup family to another (D-064)."""

    from_family: Id
    to_family: Id
    seconds: NonNegFloat


class SetupFunction(_Contract):
    """A closed set of typed setup terms, never an expression (D-064, REQ-084)."""

    id: Id
    terms: tuple[SetupTerm, ...]
    combine: Literal["sum", "max"] = "sum"
    family_matrix: tuple[FamilyMatrixEntry, ...] = ()
    default_seconds: NonNegFloat = 0.0


# --- overrides (D-066: compiled into the core model before any solver runs) ---------------


class _OverrideBase(_Contract):
    """Fields every override carries: who asked, on which fact, and why."""

    id: Id
    fact_ref: Text
    reason: Text


class LockOverride(_OverrideBase):
    """Pin an operation to a machine and/or an exact start (D-066)."""

    kind: Literal["lock"] = "lock"
    hardness: Literal["hard"]
    operation_id: Id
    machine_id: Id | None = None
    start: float | None = None


class ExpediteOverride(_OverrideBase):
    """Raise a job to weight class 4 and optionally pull its due date in (D-066)."""

    kind: Literal["expedite"] = "expedite"
    hardness: Literal["hard"]
    job_id: Id
    due: float | None = None


class PriorityOverride(_OverrideBase):
    """Set a job's weight class or explicit weight, exactly one of the two (D-066)."""

    kind: Literal["priority"] = "priority"
    hardness: Literal["hard"]
    job_id: Id
    weight_class: WeightClass | None = None
    weight: PosFloat | None = None

    @model_validator(mode="after")
    def _exactly_one_weight(self) -> Self:
        if (self.weight_class is None) == (self.weight is None):
            raise ValueError("give exactly one of weight_class or weight")
        return self


class _MachineOverrideBase(_OverrideBase):
    """Machine choice for one operation or every operation of one job, hard or soft."""

    hardness: Literal["hard", "soft"]
    operation_id: Id | None = None
    job_id: Id | None = None
    machine_id: Id
    penalty: NonNegFloat | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.operation_id is None) == (self.job_id is None):
            raise ValueError("give exactly one of operation_id or job_id")
        return self


class ExcludeMachineOverride(_MachineOverrideBase):
    """Hard: remove the machine option; soft: add `penalty` to it (D-066)."""

    kind: Literal["exclude_machine"] = "exclude_machine"


class PreferMachineOverride(_MachineOverrideBase):
    """Hard: keep only the machine option; soft: add `penalty` to the others (D-066)."""

    kind: Literal["prefer_machine"] = "prefer_machine"


class SplitOverride(_OverrideBase):
    """Split a job's lot into child jobs by positive fractions that sum to 1 (D-061, D-066)."""

    kind: Literal["split"] = "split"
    hardness: Literal["hard"]
    job_id: Id
    fractions: tuple[PosFloat, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _fractions_sum_to_one(self) -> Self:
        if abs(math.fsum(self.fractions) - 1.0) > SPLIT_SUM_TOLERANCE:
            raise ValueError("fractions must sum to 1")
        return self


class HoldOverride(_OverrideBase):
    """Delay a job until a time, or remove it from this solve when `until` is None (D-066)."""

    kind: Literal["hold"] = "hold"
    hardness: Literal["hard"]
    job_id: Id
    until: float | None = None


Override = Annotated[
    LockOverride
    | ExpediteOverride
    | PriorityOverride
    | ExcludeMachineOverride
    | PreferMachineOverride
    | SplitOverride
    | HoldOverride,
    Field(discriminator="kind"),
]


class FrozenZone(_Contract):
    """Operations of the previous plan starting before `until` keep their place (D-066)."""

    until: float
    tolerance: NonNegFloat


# --- objective specs ------------------------------------------------------------------------


class WeightedTerm(_Contract):
    """One objective in a weighted sum, optionally divided by `scale` to normalize it."""

    kind: ObjectiveKind
    weight: NonNegFloat
    scale: PosFloat | None = None


class WeightedSum(_Contract):
    """Minimize a weighted sum of objectives."""

    mode: Literal["weighted_sum"] = "weighted_sum"
    terms: tuple[WeightedTerm, ...] = Field(min_length=1)


class LexicographicLevel(_Contract):
    """One priority level; later levels may not worsen this one by more than the tolerance."""

    kind: ObjectiveKind
    tolerance: NonNegFloat = 0.0
    tolerance_kind: Literal["absolute", "relative"] = "absolute"


class Lexicographic(_Contract):
    """Optimize objectives in strict priority order."""

    mode: Literal["lexicographic"] = "lexicographic"
    levels: tuple[LexicographicLevel, ...] = Field(min_length=1)


class EpsilonBound(_Contract):
    """Upper bound on a secondary objective."""

    kind: ObjectiveKind
    upper: float


class EpsilonConstraint(_Contract):
    """Optimize `primary` subject to upper bounds on other objectives."""

    mode: Literal["epsilon_constraint"] = "epsilon_constraint"
    primary: ObjectiveKind
    bounds: tuple[EpsilonBound, ...] = ()


ObjectiveSpec = Annotated[
    WeightedSum | Lexicographic | EpsilonConstraint, Field(discriminator="mode")
]


def _default_class_weights() -> dict[int, float]:
    return {1: 1.0, 2: 2.0, 4: 4.0}


class SchedulingProfile(_Contract):
    """Plant policy: class weights, objective, frozen zone, solve budget, robustness targets.

    Field defaults reproduce today's behavior: lexicographic makespan, baseline solver.
    """

    id: Id = "default"
    class_weights: dict[int, PosFloat] = Field(default_factory=_default_class_weights)
    objective: ObjectiveSpec = Lexicographic(
        levels=(LexicographicLevel(kind=ObjectiveKind.MAKESPAN),)
    )
    frozen_zone: FrozenZone | None = None
    horizon_end: float | None = None
    time_budget_seconds: float = Field(default=10.0, gt=0, le=300)
    robustness_load_targets: tuple[LoadTarget, ...] = Field(default=(0.9, 0.8, 0.7), min_length=1)
    default_portfolio: tuple[Id, ...] = Field(default=("baseline",), min_length=1)

    @model_validator(mode="after")
    def _class_weights_cover_each_class(self) -> Self:
        if set(self.class_weights) != WEIGHT_CLASSES:
            raise ValueError("class_weights must have exactly the keys 1, 2 and 4")
        return self

    @model_validator(mode="after")
    def _load_targets_non_increasing(self) -> Self:
        targets = self.robustness_load_targets
        if any(later > earlier for earlier, later in zip(targets, targets[1:], strict=False)):
            raise ValueError("robustness_load_targets must not increase")
        return self


# --- problem and request --------------------------------------------------------------------


class Problem(_Contract):
    """The shop at `now`: jobs, machines, work centers, calendars, operator pools, setups."""

    id: Id
    now: float = 0.0
    horizon_end: float | None = None
    jobs: tuple[Job, ...] = Field(max_length=MAX_OPERATIONS)
    machines: tuple[Machine, ...] = Field(max_length=MAX_MACHINES)
    work_centers: tuple[WorkCenter, ...] = Field(max_length=MAX_MACHINES)
    calendars: tuple[Calendar, ...] = Field(default=(), max_length=MAX_MACHINES)
    operator_pools: tuple[OperatorPool, ...] = Field(default=(), max_length=MAX_MACHINES)
    setup_functions: tuple[SetupFunction, ...] = Field(default=(), max_length=MAX_MACHINES)

    @model_validator(mode="after")
    def _within_total_size_bounds(self) -> Self:
        if sum(len(job.operations) for job in self.jobs) > MAX_OPERATIONS:
            raise ValueError(f"more than {MAX_OPERATIONS} operations in total")
        if sum(len(calendar.intervals) for calendar in self.calendars) > MAX_CALENDAR_INTERVALS:
            raise ValueError(f"more than {MAX_CALENDAR_INTERVALS} calendar intervals in total")
        return self


class PreviousAssignment(_Contract):
    """Where and when an operation ran in the previous plan."""

    operation_id: Id
    machine_id: Id
    start: float


class PreviousPlan(_Contract):
    """The plan being revised; drives the frozen zone and churn."""

    plan_id: Id
    problem_digest: Id
    assignments: tuple[PreviousAssignment, ...]


class SolveRequest(_Contract):
    """The versioned input boundary: problem, profile, overrides and previous plan."""

    schema_version: Literal["2.0"]
    problem: Problem
    profile: SchedulingProfile = Field(default_factory=SchedulingProfile)
    overrides: tuple[Override, ...] = Field(default=(), max_length=MAX_OVERRIDES)
    previous_plan: PreviousPlan | None = None


# --- results --------------------------------------------------------------------------------


class ScheduleIssue(_Contract):
    """A named problem with a JSON path into the request."""

    code: Id
    path: Text
    message: Text


class Assignment(_Contract):
    """An operation placed on a machine: setup on `[start, setup_end)`, run to `end` (D-063)."""

    operation_id: Id
    machine_id: Id
    start: float
    setup_end: float
    end: float
    batch_id: Id | None = None

    @model_validator(mode="after")
    def _times_in_order(self) -> Self:
        if not self.start <= self.setup_end <= self.end:
            raise ValueError("need start <= setup_end <= end")
        return self


class Schedule(_Contract):
    """A solver's answer with its status, primary value and bound."""

    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    assignments: tuple[Assignment, ...]
    primary_value: float | None = None
    best_bound: float | None = None
    solver: Id
    elapsed_seconds: NonNegFloat
    issues: tuple[ScheduleIssue, ...] = ()


class CompiledProblem(_Contract):
    """A problem with overrides and the frozen zone applied; what solvers see (D-066)."""

    problem: Problem
    profile: SchedulingProfile
    held_job_ids: tuple[Id, ...] = ()
    released_from_frozen: tuple[Id, ...] = ()
    split_map: dict[Id, tuple[Id, ...]] = Field(default_factory=dict)
    override_issues: tuple[ScheduleIssue, ...] = ()


class VerificationResult(_Contract):
    """The independent verifier's verdict; valid only with no issues."""

    valid: bool
    issues: tuple[ScheduleIssue, ...] = ()


class SolveResult(_Contract):
    """The output boundary: schedule, objective values, verification and bookkeeping."""

    schedule: Schedule
    objective_values: dict[ObjectiveKind, float] = Field(default_factory=dict)
    verified: bool
    issues: tuple[ScheduleIssue, ...] = ()
    held_job_ids: tuple[Id, ...] = ()
    released_from_frozen: tuple[Id, ...] = ()
    features: tuple[Id, ...] = ()


def request_json_schema() -> dict[str, Any]:
    """JSON Schema of `SolveRequest`, the source of `problem.schema.json` (REQ-070)."""
    return SolveRequest.model_json_schema()
