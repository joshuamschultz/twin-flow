"""COMP-017 ModelValidator — the only safety net, run before any plan exists.

Checks run in order: schema, graph, expression, unit, field-combination; reports every
failure rather than stopping at the first. Each failure names the offending config path
(D-055, ".claude/decisions-log.md").

Inspects the RAW `model.yaml` parse tree directly — never calls
`LocationCompiler.compile()` — so a config broken enough to crash the compiler (e.g. an
`emits` entry with no `qty` and no `scrap` block) surfaces as a `ValidationError` naming
the offending path, never as an uncaught exception (D-007: fail closed on malformed
input). Per-location checks are wrapped so one broken location can never suppress the
checks run on any other location.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from twinflow.model.distributions import build_draw, build_noise
from twinflow.model.expressions import ExpressionError, ExpressionSandbox
from twinflow.model.loader import RawModel
from twinflow.primitives.part import PartTypeRegistry

RawLocation = dict[str, Any]
RawRoutingEntry = dict[str, Any]

# A Location config is schema-valid only once every one of these keys is present.
# Locations missing any of them are excluded from every later phase (graph, expression,
# unit, field-combination) rather than crashing those phases on a missing key.
_REQUIRED_LOCATION_FIELDS = (
    "name",
    "consumes",
    "emits",
    "setup_key",
    "time_model",
    "machine",
    "labor_skill",
)


@dataclass(frozen=True)
class ValidationError:
    """One validation failure, naming the offending config path."""

    path: str
    message: str


class ModelValidator:
    """RawModel -> list[ValidationError]. Runs before any plan exists (D-055)."""

    def __init__(self, registry: PartTypeRegistry, sandbox: ExpressionSandbox) -> None:
        self._registry = registry
        self._sandbox = sandbox

    def validate(self, raw_model: RawModel) -> list[ValidationError]:
        """Run schema -> graph -> expression -> unit -> field-combination checks.

        Every failure is reported; the pass never stops at the first. Empty list
        means the model may run.
        """
        data = raw_model.data
        errors: list[ValidationError] = []

        raw_locations = cast(list[RawLocation], data.get("locations", []))
        indexed_locations = list(enumerate(raw_locations))
        schema_errors, valid_locations = _schema_check_locations(indexed_locations)
        errors.extend(schema_errors)

        locations_by_name = {loc["name"]: loc for _, loc in valid_locations}

        errors.extend(_graph_checks(data, locations_by_name))

        errors.extend(_release_checks(data))

        for idx, loc in valid_locations:
            try:
                errors.extend(self._expression_checks(idx, loc))
                errors.extend(_time_model_checks(idx, loc))
                errors.extend(_capacity_checks(idx, loc))
                errors.extend(_unit_checks(idx, loc))
                errors.extend(_field_combination_checks(idx, loc))
                errors.extend(_dispatch_checks(idx, loc))
            except Exception as exc:  # noqa: BLE001 -- per-location isolation (D-007)
                errors.append(
                    ValidationError(
                        path=f"locations[{idx}]",
                        message=f"unexpected error validating location {loc.get('name')!r}: {exc}",
                    )
                )

        return errors

    def _expression_checks(self, idx: int, loc: RawLocation) -> list[ValidationError]:
        """D-055 expression check: an `attribute_scaled` `scale` naming an
        attribute not declared on `consumes[0]`'s part type is illegal."""
        time_model = loc.get("time_model")
        if not isinstance(time_model, dict) or time_model.get("kind") != "attribute_scaled":
            return []
        scale_expr = time_model.get("scale")
        if not isinstance(scale_expr, str):
            return []

        consumes = loc.get("consumes") or []
        if not consumes:
            return []  # field-combination phase reports "consumes nothing" instead

        part = consumes[0]["thing"]
        path = f"locations[{idx}].time_model.scale"
        try:
            allowed_names = set(self._registry.attributes(part))
        except KeyError:
            return []  # unknown part type is a schema/graph concern, not this check

        try:
            self._sandbox.compile(scale_expr, path, allowed_names)
        except ExpressionError as exc:
            return [ValidationError(path=path, message=str(exc))]
        return []


def _schema_check_locations(
    indexed_locations: list[tuple[int, RawLocation]],
) -> tuple[list[ValidationError], list[tuple[int, RawLocation]]]:
    """Split `locations` into schema errors and locations safe for later phases."""
    errors: list[ValidationError] = []
    valid: list[tuple[int, RawLocation]] = []
    for idx, loc in indexed_locations:
        missing = [field for field in _REQUIRED_LOCATION_FIELDS if field not in loc]
        if missing:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}]",
                    message=f"missing required field(s): {', '.join(missing)}",
                )
            )
            continue
        valid.append((idx, loc))
    return errors, valid


def _produced_things(loc: RawLocation) -> set[str]:
    """Every `thing` a location produces: its `emits` plus its `scrap` output."""
    things = {emit["thing"] for emit in loc.get("emits", [])}
    scrap = loc.get("scrap")
    if scrap is not None:
        things.add(scrap["thing"])
    return things


def _consumed_things(loc: RawLocation) -> set[str]:
    """Every `thing` a location consumes."""
    return {item["thing"] for item in loc.get("consumes", [])}


def _graph_checks(
    data: dict[str, Any], locations_by_name: dict[str, RawLocation]
) -> list[ValidationError]:
    """D-055 graph checks: reachability, missing references, orphan stock, and
    the unbounded rework/scrap-destination cycle rule."""
    errors: list[ValidationError] = []

    machine_names = {m["name"] for m in cast(list[dict[str, Any]], data.get("machines", []))}
    labor_pools = cast(dict[str, Any], data.get("labor", {})).get("pools", [])
    declared_skills = {skill for pool in labor_pools for skill in pool.get("skills", [])}

    # Per-location reference checks (machine, labor_skill) — path uses the location's
    # own index in the original `locations` list.
    errors.extend(_location_reference_checks(data, machine_names, declared_skills))

    errors.extend(_orphan_stock_checks(data, locations_by_name))
    errors.extend(_output_stock_destination_checks(data))
    errors.extend(_quality_gate_and_material_checks(data, locations_by_name))
    errors.extend(_supplier_checks(data))

    routing = cast(list[RawRoutingEntry], data.get("routing", []))
    model_max_rework = data.get("max_rework")
    for r_idx, entry in enumerate(routing):
        errors.extend(_routing_entry_checks(r_idx, entry, locations_by_name, model_max_rework))

    return errors


def _location_reference_checks(
    data: dict[str, Any], machine_names: set[str], declared_skills: set[str]
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    raw_locations = cast(list[RawLocation], data.get("locations", []))
    for idx, loc in enumerate(raw_locations):
        if "machine" in loc and loc["machine"] not in machine_names:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}].machine",
                    message=f"machine {loc['machine']!r} is not declared in top-level machines",
                )
            )
        if "labor_skill" in loc and loc["labor_skill"] not in declared_skills:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}].labor_skill",
                    message=(
                        f"labor_skill {loc['labor_skill']!r} is not declared in any labor pool"
                    ),
                )
            )
    return errors


_VALID_DISPATCH = frozenset({"fifo", "edd", "spt", "critical_ratio"})
_VALID_RELEASE = frozenset({"plan", "conwip", "wip_cap"})


def _dispatch_checks(idx: int, loc: RawLocation) -> list[ValidationError]:
    """A declared `dispatch` must be one of the four active-control rules (A1)."""
    dispatch = loc.get("dispatch")
    if dispatch is not None and dispatch not in _VALID_DISPATCH:
        return [
            ValidationError(
                path=f"locations[{idx}].dispatch",
                message=(
                    f"unknown dispatch rule {dispatch!r}; expected one of "
                    f"{sorted(_VALID_DISPATCH)}"
                ),
            )
        ]
    return []


def _release_checks(data: dict[str, Any]) -> list[ValidationError]:
    """A declared model-level `release` policy (A2) must be valid, and a capped
    policy (`conwip`/`wip_cap`) must declare a positive `wip_cap`."""
    raw = data.get("release")
    if raw is None:
        return []
    errors: list[ValidationError] = []
    policy = raw.get("policy", "plan")
    if policy not in _VALID_RELEASE:
        errors.append(
            ValidationError(
                path="release.policy",
                message=(
                    f"unknown release policy {policy!r}; "
                    f"expected one of {sorted(_VALID_RELEASE)}"
                ),
            )
        )
    if policy in ("conwip", "wip_cap"):
        wip_cap = raw.get("wip_cap")
        if wip_cap is None or not isinstance(wip_cap, int) or wip_cap < 1:
            errors.append(
                ValidationError(
                    path="release.wip_cap",
                    message=f"release policy {policy!r} requires a positive integer wip_cap",
                )
            )
    return errors


def _supplier_checks(data: dict[str, Any]) -> list[ValidationError]:
    """Supply-chain graph checks: `lead_time >= 0`, every `supplier` names a declared
    stock, and the supplier chain has no cycle (which would make an echelon its own
    upstream and never terminate at an external source)."""
    errors: list[ValidationError] = []
    stocks = cast(list[dict[str, Any]], data.get("stocks", []))
    declared = {stock["name"] for stock in stocks}
    supplier_of: dict[str, str] = {}

    for idx, stock in enumerate(stocks):
        if float(stock.get("lead_time", 0.0)) < 0.0:
            errors.append(
                ValidationError(
                    path=f"stocks[{idx}].lead_time",
                    message=f"lead_time must be >= 0, got {stock['lead_time']!r}",
                )
            )
        supplier = stock.get("supplier")
        if supplier is None:
            continue
        if supplier not in declared:
            errors.append(
                ValidationError(
                    path=f"stocks[{idx}].supplier",
                    message=f"supplier {supplier!r} is not a declared stock",
                )
            )
        else:
            supplier_of[str(stock["name"])] = str(supplier)

    for idx, stock in enumerate(stocks):
        if _has_supplier_cycle(str(stock["name"]), supplier_of):
            errors.append(
                ValidationError(
                    path=f"stocks[{idx}].supplier",
                    message=f"supplier chain from stock {stock['name']!r} forms a cycle",
                )
            )
    return errors


def _has_supplier_cycle(start: str, supplier_of: dict[str, str]) -> bool:
    """True if following `supplier` edges from `start` revisits a node."""
    seen: set[str] = set()
    current: str | None = start
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        current = supplier_of.get(current)
    return False


def _orphan_stock_checks(
    data: dict[str, Any], locations_by_name: dict[str, RawLocation]
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    referenced: set[str] = set()
    for loc in locations_by_name.values():
        referenced |= _consumed_things(loc)
        referenced |= _produced_things(loc)
        # A stock can also be referenced as a secondary `material` requirement or
        # as a `quality_gate` branch destination, not only via consumes/emits.
        material = loc.get("material")
        if isinstance(material, dict) and material.get("stock") is not None:
            referenced.add(str(material["stock"]))
        gate = loc.get("quality_gate")
        if isinstance(gate, dict):
            for branch in gate.get("branches") or []:
                if branch.get("to") is not None:
                    referenced.add(str(branch["to"]))

    stocks = cast(list[dict[str, Any]], data.get("stocks", []))
    # A stock named as another stock's upstream `supplier` is referenced, not an orphan.
    for stock in stocks:
        if stock.get("supplier") is not None:
            referenced.add(str(stock["supplier"]))

    for idx, stock in enumerate(stocks):
        if stock["name"] not in referenced:
            errors.append(
                ValidationError(
                    path=f"stocks[{idx}]",
                    message=f"stock {stock['name']!r} is never consumed or produced (orphan)",
                )
            )
    return errors


def _output_stock_destination_checks(data: dict[str, Any]) -> list[ValidationError]:
    """A location's `output_stocks[<thing>]` value must name a stock declared in
    the top-level `stocks[*].name` set (D-055 graph check)."""
    errors: list[ValidationError] = []
    declared_stock_names = {
        stock["name"] for stock in cast(list[dict[str, Any]], data.get("stocks", []))
    }

    raw_locations = cast(list[RawLocation], data.get("locations", []))
    for idx, loc in enumerate(raw_locations):
        output_stocks = cast(dict[str, str], loc.get("output_stocks") or {})
        for thing, stock_name in output_stocks.items():
            if stock_name not in declared_stock_names:
                errors.append(
                    ValidationError(
                        path=f"locations[{idx}].output_stocks.{thing}",
                        message=(
                            f"output_stocks names stock {stock_name!r}, which is not "
                            f"declared in top-level stocks"
                        ),
                    )
                )
    return errors


def _quality_gate_and_material_checks(
    data: dict[str, Any], locations_by_name: dict[str, RawLocation]
) -> list[ValidationError]:
    """Tier 0 graph checks: a probabilistic `quality_gate`'s branch probabilities
    must sum to 1.0 and every branch `to` must name a declared location or stock
    (or be null); a `material` requirement's `stock` must be a declared stock."""
    errors: list[ValidationError] = []
    declared_stock_names = {
        stock["name"] for stock in cast(list[dict[str, Any]], data.get("stocks", []))
    }
    raw_locations = cast(list[RawLocation], data.get("locations", []))
    for idx, loc in enumerate(raw_locations):
        gate = loc.get("quality_gate")
        if isinstance(gate, dict):
            branches = gate.get("branches") or []
            try:
                total = sum(float(branch.get("prob", 0.0)) for branch in branches)
            except (TypeError, ValueError):
                total = float("nan")
            if not branches or abs(total - 1.0) > 1e-6:
                errors.append(
                    ValidationError(
                        path=f"locations[{idx}].quality_gate",
                        message=f"branch probabilities must sum to 1.0, got {total}",
                    )
                )
            for b_idx, branch in enumerate(branches):
                target = branch.get("to")
                if (
                    target is not None
                    and target not in locations_by_name
                    and target not in declared_stock_names
                ):
                    errors.append(
                        ValidationError(
                            path=f"locations[{idx}].quality_gate.branches[{b_idx}].to",
                            message=(
                                f"names unknown target {target!r} "
                                f"(not a declared location or stock)"
                            ),
                        )
                    )

        material = loc.get("material")
        if isinstance(material, dict) and material.get("stock") not in declared_stock_names:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}].material.stock",
                    message=(
                        f"names stock {material.get('stock')!r}, which is not "
                        f"declared in top-level stocks"
                    ),
                )
            )
    return errors


def _routing_entry_checks(
    r_idx: int,
    entry: RawRoutingEntry,
    locations_by_name: dict[str, RawLocation],
    model_max_rework: Any,
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    steps = entry.get("steps", [])
    finished_part = entry.get("part")

    visited: set[str] = set()
    for s_idx, step in enumerate(steps):
        if step not in locations_by_name:
            errors.append(
                ValidationError(
                    path=f"routing[{r_idx}].steps[{s_idx}]",
                    message=f"step names location {step!r}, which is not declared",
                )
            )
            continue

        if step in visited:
            loc = locations_by_name[step]
            bound = loc.get("max_rework", model_max_rework)
            if bound is None:
                errors.append(
                    ValidationError(
                        path=f"routing[{r_idx}].steps[{s_idx}]",
                        message=(
                            f"step closes an unbounded rework/scrap-destination cycle back "
                            f"to {step!r} with no declared max_rework bound"
                        ),
                    )
                )
        visited.add(step)

    last_step = steps[-1] if steps else None
    last_loc = locations_by_name.get(last_step) if last_step is not None else None
    if last_loc is not None and finished_part not in _produced_things(last_loc):
        errors.append(
            ValidationError(
                path=f"routing[{r_idx}]",
                message=(
                    f"routing never reaches the finished part {finished_part!r}: "
                    f"last step {last_step!r} does not emit or scrap it"
                ),
            )
        )

    return errors


def _basis_uom(loc: RawLocation) -> str | None:
    consumes = loc.get("consumes") or []
    if not consumes:
        return None
    return str(consumes[0]["uom"])


def _capacity_checks(idx: int, loc: RawLocation) -> list[ValidationError]:
    """A declared `capacity` (parallel machines at the center, COMP-030) must be a
    positive integer. Absent means one machine."""
    if "capacity" not in loc:
        return []
    capacity = loc["capacity"]
    ok = isinstance(capacity, int) and not isinstance(capacity, bool) and capacity >= 1
    if ok:
        return []
    return [
        ValidationError(
            path=f"locations[{idx}].capacity",
            message=f"capacity must be a positive integer, got {capacity!r}",
        )
    ]


def _time_model_checks(idx: int, loc: RawLocation) -> list[ValidationError]:
    """Pre-flight the declared cycle-time variation (COMP-038): a `cv` must be a
    non-negative number, and a `distribution` must name a known shape with valid
    params. Reuses the compiler's own builders so the rules live in one place."""
    time_model = loc.get("time_model")
    if not isinstance(time_model, dict):
        return []

    errors: list[ValidationError] = []
    path = f"locations[{idx}].time_model"

    if "cv" in time_model:
        try:
            build_noise(float(time_model["cv"]))
        except (ValueError, TypeError) as exc:
            errors.append(ValidationError(path=f"{path}.cv", message=str(exc)))

    if time_model.get("kind") == "distribution":
        spec = {k: v for k, v in time_model.items() if k not in {"kind", "load", "unload"}}
        try:
            build_draw(spec)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(ValidationError(path=path, message=f"invalid distribution: {exc}"))

    return errors


def _unit_checks(idx: int, loc: RawLocation) -> list[ValidationError]:
    """D-055 rule 3: a uom-changing emit that omits its own `qty` is illegal."""
    basis_uom = _basis_uom(loc)
    if basis_uom is None:
        return []  # field-combination phase reports "consumes nothing" instead

    errors: list[ValidationError] = []
    for e_idx, emit in enumerate(loc.get("emits", [])):
        uom_changed = emit.get("uom") != basis_uom
        omits_qty = emit.get("qty") is None
        if uom_changed and omits_qty:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}].emits[{e_idx}]",
                    message=(
                        f"emit uom {emit.get('uom')!r} differs from the basis uom "
                        f"{basis_uom!r} but omits its own qty"
                    ),
                )
            )
    return errors


def _field_combination_checks(idx: int, loc: RawLocation) -> list[ValidationError]:
    """D-055 rules 1 and 2: consumes-nothing, produces-nothing, and a `scrap`
    block paired with anything other than exactly one bare emit."""
    errors: list[ValidationError] = []

    consumes = loc.get("consumes") or []
    emits = loc.get("emits") or []
    scrap = loc.get("scrap")

    if not consumes:
        errors.append(
            ValidationError(
                path=f"locations[{idx}]",
                message="location consumes nothing: at least one consumes entry is required",
            )
        )

    if not emits and scrap is None:
        errors.append(
            ValidationError(
                path=f"locations[{idx}]",
                message="location produces nothing: at least one emit or a scrap block is required",
            )
        )

    if scrap is not None:
        bare_emit_count = sum(1 for emit in emits if emit.get("qty") is None)
        if bare_emit_count != 1:
            errors.append(
                ValidationError(
                    path=f"locations[{idx}].emits",
                    message=(
                        f"scrap is present so exactly one emit must be bare (the "
                        f"remainder); found {bare_emit_count}"
                    ),
                )
            )

    return errors
