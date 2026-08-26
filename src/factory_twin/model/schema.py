"""Schema definitions for the model surface (part types, stocks, locations, ...)."""

from __future__ import annotations

from dataclasses import dataclass

from factory_twin.primitives.bundle import Bundle
from factory_twin.primitives.cell import Machine, SetupPolicy
from factory_twin.primitives.location import MaterialRequirementLike, PullRule
from factory_twin.primitives.part import PartTypeRegistry
from factory_twin.primitives.stock import Stock
from factory_twin.primitives.time_model import TimeModel
from factory_twin.primitives.transform import Transform


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
