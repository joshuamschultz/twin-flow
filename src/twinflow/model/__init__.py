"""Layer 2 — config in, routing graph + BOM out. The only YAML/expression trust boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from twinflow.model.compile import LocationCompiler
from twinflow.model.expressions import ExpressionSandbox
from twinflow.model.loader import RawModel, load_raw_model
from twinflow.model.schema import LocationSpec
from twinflow.model.validate import ModelValidator, ValidationError
from twinflow.primitives.part import PartTypeRegistry, PartTypeSpec

__all__ = [
    "CompiledModel",
    "LaborPoolConfig",
    "ModelValidationError",
    "StockConfig",
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
class StockConfig:
    """Raw declarative shape of one top-level `stocks[*]` entry (model.yaml's
    `stocks` section). Not a runtime `primitives.stock.Stock` — that is
    env-bound and built fresh every `RunDriver.run()` call, never here, so a
    `CompiledModel` can only carry the declarative shape a driver later builds
    a fresh `Stock` from (D-044), mirroring `LaborPoolConfig`/`labor_pools`. A
    stock's `name` doubles as its material identity (`Stock.put()` checks
    thing match), so there is no separate `thing` field.

    `initial` seeds the run-bound `Stock`'s level (default 0.0, historical v1). A
    `reorder_point`/`refill_to` pair declares a self-refilling stock: when a pull
    would drop the level below `reorder_point`, the driver places ONE replenishment
    order (order-up-to `refill_to`) that arrives after `lead_time` seconds
    (`lead_time: 0.0`, the default, keeps the historical instantaneous refill). While
    an order is in transit no second order is placed (an (s, S) policy). If
    `supplier` names another stock, the ordered quantity is pulled from that upstream
    stock (a multi-echelon chain) rather than an infinite external source."""

    name: str
    uom: str
    initial: float = 0.0
    reorder_point: float | None = None
    refill_to: float | None = None
    lead_time: float = 0.0
    supplier: str | None = None


@dataclass(frozen=True)
class CompiledModel:
    """`load_model()`'s return value.

    Carries `model.compile.CompileResult`'s three fields (`locations`, `routing`,
    `bom`) straight through, plus the raw `labor.pools` shape, the raw
    top-level `stocks` shape, and the `PartTypeRegistry` a plan-layer
    `RunDriver` needs but `LocationCompiler` never builds (it only ever sets
    `material_requirement=None` and never reads the model's top-level `labor`
    or `stocks` sections).
    """

    locations: list[LocationSpec]
    routing: dict[str, list[str]]
    bom: dict[str, dict[str, float]]
    labor_pools: list[LaborPoolConfig]
    registry: PartTypeRegistry
    stocks: list[StockConfig] = field(default_factory=list)


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
    stocks = _build_stocks(raw_model)

    return CompiledModel(
        locations=result.locations,
        routing=result.routing,
        bom=result.bom,
        labor_pools=labor_pools,
        stocks=stocks,
        registry=registry,
    )


def validate_model(path: str) -> list[ValidationError]:
    """Public API: parse a model.yaml and run every validation check WITHOUT raising.

    The non-raising counterpart to `load_model`, for `twinflow validate`: it reports
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


def _build_stocks(raw_model: RawModel) -> list[StockConfig]:
    """Carry `model.yaml`'s top-level `stocks` declarative shape through
    unchanged, so a later `RunDriver` can build a fresh, run-bound `Stock` per
    entry (D-044) -- mirrors `_build_labor_pools`."""
    stocks = cast(list[dict[str, Any]], raw_model.stocks)
    return [
        StockConfig(
            name=str(stock["name"]),
            uom=str(stock["uom"]),
            initial=float(stock.get("initial", 0.0)),
            reorder_point=(
                float(stock["reorder_point"]) if stock.get("reorder_point") is not None else None
            ),
            refill_to=(
                float(stock["refill_to"]) if stock.get("refill_to") is not None else None
            ),
            lead_time=float(stock.get("lead_time", 0.0)),
            supplier=(str(stock["supplier"]) if stock.get("supplier") is not None else None),
        )
        for stock in stocks
    ]
