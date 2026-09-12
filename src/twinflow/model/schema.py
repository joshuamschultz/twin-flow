"""Schema definitions for the model surface (part types, stocks, locations, ...)."""

from __future__ import annotations

from dataclasses import dataclass, field

from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine, SetupPolicy
from twinflow.primitives.location import MaterialRequirementLike, PullRule, RoutingPolicy
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock
from twinflow.primitives.time_model import TimeModel
from twinflow.primitives.transform import Transform


@dataclass(frozen=True)
class QualityBranchSpec:
    """One branch of a probabilistic quality gate: the chance `prob` that a whole
    firing's output routes to `to` (a location name, a stock name, or `None` for a
    terminal sink / finished part)."""

    prob: float
    to: str | None


@dataclass(frozen=True)
class QualityGateSpec:
    """A compiled probabilistic quality gate (COMP-016). `thing` names the emitted
    output that is gated; `branches` partition it by chance and must sum to 1.0
    (checked in model/validate.py). `RunDriver` resolves each branch's `to` into a
    run-bound sink and builds the `RoutingPolicy` the Location draws against."""

    thing: str
    branches: list[QualityBranchSpec]


@dataclass(frozen=True)
class BreakdownSpec:
    """A compiled machine-breakdown disruption (A4): the center's machine fails on a
    seeded schedule with mean time between failures `mtbf_seconds`, then is unavailable
    for a repair time drawn from `mttr` (a `time_model`-style spec compiled to a draw
    callable). Each run's breakdown process draws from the dedicated SOURCE_BREAKDOWN
    stream, so downtime is reproducible and pairs across replications (D-033)."""

    mtbf_seconds: float
    mttr_draw: object
    """A `Callable[[np.random.Generator], float]` built by the compiler from the `mttr`
    distribution spec; typed `object` here to keep the schema numpy-free."""


@dataclass(frozen=True)
class MaterialSpec:
    """A compiled secondary-material requirement (COMP-016): the location pulls
    `qty` of stock `stock` (in `uom`) at step 3 of the order of operations, on top
    of its step-1 queue input. `RunDriver` binds this declarative shape to a fresh,
    env-bound `Stock`, mirroring `stock_destinations`/`labor_pools` (D-044)."""

    stock: str
    qty: float
    uom: str


@dataclass
class LocationSpec:
    """Concrete `primitives.location.LocationSpecLike` (COMP-016 compiler output).

    `LocationCompiler` is the only thing that constructs this. Primitives never
    see the YAML surface, only this already-compiled shape (D-044).
    """

    location_id: str
    machine: Machine
    setup_policy: SetupPolicy
    pull_rule: PullRule
    time_model: TimeModel
    transform: Transform
    registry: PartTypeRegistry
    labor_skill: str
    material_requirement: MaterialRequirementLike | None
    destinations: dict[str, Stock | list[Bundle]]
    stock_destinations: dict[str, str] = field(default_factory=dict)
    """Output `thing` -> declared top-level Stock NAME (D-044 general config
    sugar), never a runtime `Stock` object (`LocationCompiler.compile()` is not
    env-bound; only `RunDriver.run()` is). Empty when the location declares no
    `output_stocks`. `RunDriver` resolves each entry into a fresh, run-bound
    `Stock` and rewrites `destinations[thing]` to it, mirroring routing's own
    post-compile `destinations` rewrite."""

    capacity: int = 1
    """How many identical machines run in parallel at this center (COMP-030).
    `RunDriver` sizes the location's machine pool to this, so up to `capacity`
    jobs process concurrently. Default 1 (a single machine)."""

    quality_gate: QualityGateSpec | None = None
    """A declared probabilistic quality gate (Tier 0), or None. `RunDriver`
    resolves it into `routers` at run time; the compiler never binds env state."""

    material_spec: MaterialSpec | None = None
    """A declared secondary-material requirement (Tier 0), or None. `RunDriver`
    binds it to a fresh `Stock` and sets `material_requirement`."""

    routers: dict[str, RoutingPolicy] = field(default_factory=dict)
    """Runtime-only: output `thing` -> `RoutingPolicy` over run-bound sinks, built
    fresh by `RunDriver` from `quality_gate`. Empty at compile time; the Location
    reads it via `getattr` so compile-time and test-double specs need not set it."""

    dispatch: str = "fifo"
    """Active-control dispatch rule (A1): which queued job this center runs next —
    one of `fifo` (arrival order, the default), `edd`, `spt`, `critical_ratio`. A
    dot-path lever (`locations[<name>].dispatch`) so a sweep/optimize can compare
    policies. The Location reads it via `getattr`, so test-double specs need not set it."""

    breakdown: BreakdownSpec | None = None
    """A declared machine-breakdown disruption (A4), or None. `RunDriver` starts a
    seeded failure process from it that makes the machine unavailable for repair."""

    shift_crossing: str = "overtime"
    """How setup/run work behaves at shift end: pause, finish_unattended, overtime."""
