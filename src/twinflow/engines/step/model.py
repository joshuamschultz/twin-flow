"""COMP-150: parse and validate a `domain: make_to_stock` model (REQ-195, REQ-213).

```yaml
domain: make_to_stock
period: day
horizon: 60
start_cover_days: 8
line:
  id: press
  changeover:
    within_family: {cost: 200, output_lost: 0.10}
    across_family: {cost: 1200, output_lost: 0.50}
products:
  - {id: GRY-S, family: grey, abc: A, demand: 60, run_rate: 450, price: 12.0,
     unit_cost: 7.2, max_storage: 1500}
economics:
  defaults: {holding_rate: 0.01, lost_sale_rate: 0.30}
```

Exactly one line (D-132). Product price and unit cost are the model's own; holding and
lost-sale cost come from `economics` (rate x unit cost, rate x price, D-133). Every problem
is an `Issue` naming the field and how to fix it; parsing never raises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from twinflow.state.economics import Economics, parse_economics
from twinflow.state.groups import GroupedView, parse_state_groups
from twinflow.state.issues import Issue, did_you_mean, number_or_issue, unknown_keys

ROOT_KEYS = (
    "domain",
    "revision",
    "period",
    "horizon",
    "start_cover_days",
    "line",
    "families",
    "products",
    "economics",
    "state_groups",
)
PRODUCT_KEYS = (
    "id",
    "family",
    "abc",
    "demand",
    "run_rate",
    "price",
    "unit_cost",
    "max_storage",
)
ABC_CLASSES = ("A", "B", "C")
_CHANGEOVER_KEYS = ("cost", "output_lost")


@dataclass(frozen=True)
class Changeover:
    """What a product switch costs: dollars and the share of the day's output lost."""

    cost: float
    output_lost: float


@dataclass(frozen=True)
class Product:
    """One product on the line. `demand_rate` is mean units per day (Poisson)."""

    id: str
    family: str
    abc: str
    demand_rate: float
    run_rate: float
    price: float
    unit_cost: float
    max_storage: int


@dataclass(frozen=True)
class StepModel:
    """A validated make-to-stock model, products in model order."""

    line_id: str
    horizon: int
    start_cover_days: float
    within: Changeover
    across: Changeover
    products: tuple[Product, ...]
    economics: Economics
    views: Mapping[str, GroupedView]
    revision: str = "1"

    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.products)

    @property
    def families(self) -> tuple[str, ...]:
        return tuple(p.family for p in self.products)

    @property
    def demand_rates(self) -> tuple[float, ...]:
        return tuple(p.demand_rate for p in self.products)

    def holding_cost(self, product_id: str) -> float:
        """Dollars to keep one unit one night (rate x unit cost unless set per product)."""
        return self.economics.thing(product_id).holding_per_unit_day

    def lost_sale_cost(self, product_id: str) -> float:
        """Dollars of penalty for one unit of unmet demand (rate x price unless set)."""
        return self.economics.thing(product_id).lost_sale_cost


def _error(path: str, message: str, suggestion: str) -> Issue:
    return Issue(path, message, "error", suggestion)


def _number(
    raw: Mapping[Any, Any],
    key: str,
    path: str,
    issues: list[Issue],
    *,
    positive: bool = False,
    whole: bool = False,
    at_most: float | None = None,
    default: float | None = None,
) -> float | None:
    """A checked number from `raw[key]`; an error issue (and None) when it is not valid."""
    where = f"{path}.{key}"
    if key not in raw:
        if default is not None:
            return default
        issues.append(_error(where, f"{key} is missing.", f"Add {key}: <number>."))
        return None
    value, issue = number_or_issue(raw[key], where)
    if issue is not None or value is None:
        if issue is not None:
            issues.append(issue)
        return None
    if positive and value <= 0:
        issues.append(
            _error(where, f"{key} must be more than 0, not {value:g}.", "Use a positive number.")
        )
        return None
    if whole and not float(value).is_integer():
        issues.append(_error(where, f"{key} must be a whole number, not {value:g}.", "Round it."))
        return None
    if at_most is not None and value > at_most:
        issues.append(
            _error(
                where, f"{key} must be between 0 and {at_most:g}, not {value:g}.", "Use a share."
            )
        )
        return None
    return value


def _changeover(raw: object, path: str, issues: list[Issue]) -> Changeover | None:
    if not isinstance(raw, Mapping):
        issues.append(
            _error(
                path,
                "This needs a cost and an output_lost.",
                "Write {cost: 200, output_lost: 0.1}.",
            )
        )
        return None
    issues.extend(unknown_keys(dict(raw), _CHANGEOVER_KEYS, path))
    cost = _number(raw, "cost", path, issues)
    lost = _number(raw, "output_lost", path, issues, at_most=1.0)
    return Changeover(cost, lost) if cost is not None and lost is not None else None


def _line(
    raw: Mapping[str, Any], path: str, issues: list[Issue]
) -> tuple[str, Changeover, Changeover] | None:
    """The single line: its id and its two changeover kinds."""
    if "lines" in raw or isinstance(raw.get("line"), list):
        issues.append(
            _error(
                f"{path}.line",
                "A make-to-stock model has one line.",
                "Keep one line under 'line:'. Several lines come in a later release.",
            )
        )
        return None
    line = raw.get("line")
    if not isinstance(line, Mapping):
        issues.append(
            _error(
                f"{path}.line",
                "The model has no line.",
                "Add line: {id: press, changeover: {...}}.",
            )
        )
        return None
    where = f"{path}.line"
    issues.extend(unknown_keys(dict(line), ("id", "changeover"), where))
    line_id = line.get("id")
    if not isinstance(line_id, str) or not line_id:
        issues.append(_error(f"{where}.id", "The line needs a name.", "Write id: press."))
        return None
    changeover = line.get("changeover")
    if not isinstance(changeover, Mapping):
        issues.append(
            _error(
                f"{where}.changeover",
                "The line has no changeover.",
                "Add within_family and across_family.",
            )
        )
        return None
    within = _changeover(
        changeover.get("within_family"), f"{where}.changeover.within_family", issues
    )
    across = _changeover(
        changeover.get("across_family"), f"{where}.changeover.across_family", issues
    )
    return (line_id, within, across) if within and across else None


def _text(raw: Mapping[Any, Any], key: str, path: str, issues: list[Issue]) -> str | None:
    value = raw.get(key)
    if isinstance(value, str) and value:
        return value
    issues.append(_error(f"{path}.{key}", f"{key} is missing.", f"Write {key}: <name>."))
    return None


def _product(
    raw: object,
    index: int,
    seen: set[str],
    families: Sequence[str] | None,
    issues: list[Issue],
) -> Product | None:
    path = f"model.products[{index}]"
    if not isinstance(raw, Mapping):
        issues.append(
            _error(
                path, "A product must be a set of named fields.", "Write {id: ..., demand: ...}."
            )
        )
        return None
    before = len(issues)
    issues.extend(unknown_keys(dict(raw), PRODUCT_KEYS, path))
    product_id = _text(raw, "id", path, issues)
    family = _text(raw, "family", path, issues)
    if product_id is not None and product_id in seen:
        issues.append(
            _error(
                f"{path}.id",
                f"The product id {product_id} is used twice.",
                "Give each product its own id.",
            )
        )
    if family is not None and families is not None and family not in families:
        issues.append(
            _error(
                f"{path}.family",
                f"The family {family} is not in the families list.",
                did_you_mean(family, families),
            )
        )
    abc = raw.get("abc", "B")
    if abc not in ABC_CLASSES:
        issues.append(
            _error(f"{path}.abc", f"abc must be A, B or C, not {abc!r}.", "Write abc: A.")
        )
    numbers = {
        "demand": _number(raw, "demand", path, issues, positive=True),
        "run_rate": _number(raw, "run_rate", path, issues, positive=True),
        "price": _number(raw, "price", path, issues),
        "unit_cost": _number(raw, "unit_cost", path, issues),
        "max_storage": _number(raw, "max_storage", path, issues, positive=True, whole=True),
    }
    if len(issues) > before or product_id is None or family is None:
        return None
    seen.add(product_id)
    return Product(
        product_id,
        family,
        str(abc),
        float(numbers["demand"] or 0.0),
        float(numbers["run_rate"] or 0.0),
        float(numbers["price"] or 0.0),
        float(numbers["unit_cost"] or 0.0),
        int(numbers["max_storage"] or 0),
    )


def _families(raw: Mapping[str, Any], issues: list[Issue]) -> list[str] | None:
    declared = raw.get("families")
    if declared is None:
        return None
    if not isinstance(declared, list) or not all(isinstance(f, str) and f for f in declared):
        issues.append(
            _error(
                "model.families",
                "families must be a list of names.",
                "Write families: [grey, black].",
            )
        )
        return None
    return list(declared)


def _products(raw: Mapping[str, Any], issues: list[Issue]) -> tuple[Product, ...] | None:
    listed = raw.get("products")
    if not isinstance(listed, list) or not listed:
        issues.append(
            _error("model.products", "The model has no products.", "Add at least one product.")
        )
        return None
    families = _families(raw, issues)
    seen: set[str] = set()
    found = [_product(item, i, seen, families, issues) for i, item in enumerate(listed)]
    return tuple(p for p in found if p is not None) if all(found) else None


def _economics(
    raw: Mapping[str, Any],
    products: tuple[Product, ...],
    within: Changeover,
    across: Changeover,
    issues: list[Issue],
) -> Economics:
    """Merge the model's own price, unit cost and changeover dollars into the section."""
    given = raw.get("economics")
    section: dict[str, Any] = dict(given) if isinstance(given, Mapping) else {}
    if given is not None and not isinstance(given, Mapping):
        issues.append(
            _error(
                "model.economics",
                "economics must be a set of named fields.",
                "See the economics how-to.",
            )
        )
    things = dict(section["things"]) if isinstance(section.get("things"), Mapping) else {}
    for product in products:
        entry = dict(things[product.id]) if isinstance(things.get(product.id), Mapping) else {}
        entry.update(price=product.price, unit_cost=product.unit_cost)
        things[product.id] = entry
    section["things"] = things
    section["setup"] = {"within_family": within.cost, "across_family": across.cost}
    economics, found = parse_economics(section, tuple(p.id for p in products), "model.economics")
    issues.extend(found)
    return economics


def _header(raw: Mapping[str, Any], issues: list[Issue]) -> tuple[int, float] | None:
    issues.extend(
        unknown_keys(cast("dict[object, object]", dict(raw)), ROOT_KEYS + ("lines",), "model")
    )
    if raw.get("period", "day") != "day":
        issues.append(
            _error(
                "model.period", f"period {raw['period']!r} is not supported.", "Use period: day."
            )
        )
    horizon = _number(raw, "horizon", "model", issues, positive=True, whole=True)
    cover = _number(raw, "start_cover_days", "model", issues, default=8.0)
    if horizon is None or cover is None:
        return None
    return int(horizon), cover


def parse_step_model(raw: Mapping[str, Any]) -> tuple[StepModel | None, list[Issue]]:
    """The model and its issues; the model is None when any issue is an error."""
    if not isinstance(raw, Mapping):
        return None, [
            _error("model", "The model must be a set of named fields.", "Start from the example.")
        ]
    issues: list[Issue] = []
    header = _header(raw, issues)
    line = _line(raw, "model", issues)
    products = _products(raw, issues)
    if line is None or products is None or header is None:
        return None, issues
    economics = _economics(raw, products, line[1], line[2], issues)
    ids = tuple(p.id for p in products)
    views, view_issues = parse_state_groups(raw.get("state_groups"), ids, "model.state_groups")
    issues.extend(view_issues)
    if any(i.severity == "error" for i in issues):
        return None, issues
    model = StepModel(
        line_id=line[0],
        horizon=header[0],
        start_cover_days=header[1],
        within=line[1],
        across=line[2],
        products=products,
        economics=economics,
        views=MappingProxyType(views),
        revision=str(raw.get("revision", "1")),
    )
    return model, issues
