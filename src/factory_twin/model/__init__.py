"""Layer 2 — config in, routing graph + BOM out. The only YAML/expression trust boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from factory_twin.model.compile import LocationCompiler
from factory_twin.model.expressions import ExpressionSandbox
from factory_twin.model.loader import RawModel, load_raw_model
from factory_twin.model.schema import LocationSpec
from factory_twin.model.validate import ModelValidator, ValidationError
from factory_twin.primitives.part import PartTypeRegistry, PartTypeSpec

__all__ = [
    "CompiledModel",
    "LaborPoolConfig",
    "ModelValidationError",
    "load_model",
    "validate_model",
]

# Project-imposed expression depth/length limits (D-002, D-006; OWASP baseline in
# tech.md). Matches the values every existing fixture (test_compile.py,
# test_validate.py) already builds its ExpressionSandbox with.
MAX_EXPRESSION_DEPTH = 10
MAX_EXPRESSION_LENGTH = 200


@dataclass(frozen=True)
class LaborPoolConfig:
    """Raw declarative shape of one `labor.pools` entry (model.yaml's `labor`
    section). Not a runtime `primitives.labor.LaborPool` — that is env-bound and
    RunContext/env is built fresh every `RunDriver.run()` call, never here, so a
    `CompiledModel` can only carry the declarative shape a driver later builds a
    fresh `LaborPool` from (COMP-012, COMP-019)."""

    name: str
    headcount: int
    skills: frozenset[str]


@dataclass(frozen=True)
class CompiledModel:
    """`load_model()`'s return value.

    Carries `model.compile.CompileResult`'s three fields (`locations`, `routing`,
    `bom`) straight through, plus the raw `labor.pools` shape and the
    `PartTypeRegistry` a plan-layer `RunDriver` needs but `LocationCompiler`
    never builds (it only ever sets `material_requirement=None` and never reads
    the model's top-level `labor` section).
    """

    locations: list[LocationSpec]
    routing: dict[str, list[str]]
    bom: dict[str, dict[str, float]]
    labor_pools: list[LaborPoolConfig]
    registry: PartTypeRegistry


class ModelValidationError(ValueError):
    """Raised by `load_model` when `ModelValidator` reports one or more errors."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        joined = "; ".join(f"{error.path}: {error.message}" for error in errors)
        super().__init__(f"model validation failed ({len(errors)} error(s)): {joined}")


def load_model(path: str) -> CompiledModel:
    """Public API: parse + validate + compile a model.yaml into a CompiledModel.

    Parses exactly once (`model.loader.load_raw_model`), then validates
    (`ModelValidator`) before compiling (`LocationCompiler`) — never the reverse
    order — so a config broken enough to crash the compiler still surfaces as a
    `ModelValidationError` naming the offending path rather than an uncaught
    exception (D-007, `model/validate.py`'s own documented guarantee: it inspects
    the raw parse tree and never calls `LocationCompiler.compile()` for exactly
    this reason). Raises `ModelValidationError` if validation reports any error;
    compiles exactly once otherwise.
    """
    raw_model = load_raw_model(path)
    registry = _build_registry(raw_model)
    sandbox = ExpressionSandbox(max_depth=MAX_EXPRESSION_DEPTH, max_length=MAX_EXPRESSION_LENGTH)

    errors = ModelValidator(registry, sandbox).validate(raw_model)
    if errors:
        raise ModelValidationError(errors)

    result = LocationCompiler(registry, sandbox).compile(raw_model)
    labor_pools = _build_labor_pools(raw_model)

    return CompiledModel(
        locations=result.locations,
        routing=result.routing,
        bom=result.bom,
        labor_pools=labor_pools,
        registry=registry,
    )


def validate_model(path: str) -> list[ValidationError]:
    """Public API: parse a model.yaml and run every validation check WITHOUT raising.

    The non-raising counterpart to `load_model`, for `ftwin validate`: it reports
    every `ValidationError` (empty list means the model may run) rather than
    stopping at the first or raising a `ModelValidationError`. Never compiles — the
    validator inspects the raw parse tree directly (D-007).
    """
    raw_model = load_raw_model(path)
    registry = _build_registry(raw_model)
    sandbox = ExpressionSandbox(max_depth=MAX_EXPRESSION_DEPTH, max_length=MAX_EXPRESSION_LENGTH)
    return ModelValidator(registry, sandbox).validate(raw_model)


def _build_registry(raw_model: RawModel) -> PartTypeRegistry:
    """Build the `PartTypeRegistry` from `model.yaml`'s `part_types` section."""
    part_types = cast(list[dict[str, Any]], raw_model.part_types)
    specs: dict[str, PartTypeSpec] = {
        str(part_type["name"]): {
            "attributes": cast(dict[str, type], part_type.get("attributes", {})),
            "uom": str(part_type["uom"]),
        }
        for part_type in part_types
    }
    return PartTypeRegistry(specs)


def _build_labor_pools(raw_model: RawModel) -> list[LaborPoolConfig]:
    """Carry `model.yaml`'s `labor.pools` declarative shape through unchanged."""
    labor = cast(dict[str, Any], raw_model.labor)
    pools = cast(list[dict[str, Any]], labor.get("pools", []))
    return [
        LaborPoolConfig(
            name=str(pool["name"]),
            headcount=int(pool["headcount"]),
            skills=frozenset(str(skill) for skill in pool.get("skills", [])),
        )
        for pool in pools
    ]
