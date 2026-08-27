"""Schema definitions for the model surface (part types, stocks, locations, ...)."""

from __future__ import annotations

from dataclasses import dataclass, field

from twinflow.primitives.bundle import Bundle
from twinflow.primitives.cell import Machine, SetupPolicy
from twinflow.primitives.location import MaterialRequirementLike, PullRule
from twinflow.primitives.part import PartTypeRegistry
from twinflow.primitives.stock import Stock
from twinflow.primitives.time_model import TimeModel
from twinflow.primitives.transform import Transform


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
