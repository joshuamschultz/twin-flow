"""COMP-154: the state a policy file reads, with plain names (REQ-202).

`policy_state(snapshot, facts, actions)` joins one `Snapshot` with the model's static facts
(`ModelFacts`) into a read-only `PolicyState`:

- `state.products`             the product ids, in model order
- `state.stock[p]`             units on hand
- `state.days_of_cover[p]`     days the stock lasts at the usual sales rate
- `state.demand_rate[p]`       usual units sold per day
- `state.family[p]`, `state.abc[p]`
- `state.price[p]`, `state.unit_cost[p]`, `state.margin[p]` (price less unit cost)
- `state.max_storage[p]`, `state.run_rate[p]`
- `state.holding_cost[p]`      dollars to keep one unit one night
- `state.lost_sale_cost[p]`    dollars lost per unit of unmet demand
- `state.changeover`           `{"within_family": {"cost", "output_lost"}, "across_family": ...}`
- `state.setup`                the product id the line is set up for (one line, D-132)
- `state.period`, `state.horizon`, `state.days_left`
- `state.actions`              what `decide` may return
- `state.features`             values added by feature files (REQ-219)

A `DECISION = "dispatch"` file gets a `DispatchState` instead: `location`, `queue` (the
waiting jobs as read-only dicts), `actions` (the rule names), `time` and `current`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from twinflow.engines.step.model import StepModel
from twinflow.policy import DISPATCH_NAMES
from twinflow.state.records import Snapshot

_EMPTY: Mapping[str, Any] = MappingProxyType({})


def _frozen(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


def _frozen_nested(values: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Mapping[str, Any]]:
    return MappingProxyType({key: _frozen(inner) for key, inner in values.items()})


@dataclass(frozen=True)
class ModelFacts:
    """What does not change during a run: the products and their numbers."""

    products: tuple[str, ...] = ()
    family: Mapping[str, str] = field(default=_EMPTY)
    abc: Mapping[str, str] = field(default=_EMPTY)
    demand_rate: Mapping[str, float] = field(default=_EMPTY)
    price: Mapping[str, float] = field(default=_EMPTY)
    unit_cost: Mapping[str, float] = field(default=_EMPTY)
    run_rate: Mapping[str, float] = field(default=_EMPTY)
    max_storage: Mapping[str, int] = field(default=_EMPTY)
    holding_cost: Mapping[str, float] = field(default=_EMPTY)
    lost_sale_cost: Mapping[str, float] = field(default=_EMPTY)
    changeover: Mapping[str, Mapping[str, float]] = field(default=_EMPTY)
    horizon: int = 0
    line: str | None = None

    def __post_init__(self) -> None:
        put = object.__setattr__
        put(self, "products", tuple(self.products))
        for name in (
            "family",
            "abc",
            "demand_rate",
            "price",
            "unit_cost",
            "run_rate",
            "max_storage",
            "holding_cost",
            "lost_sale_cost",
        ):
            put(self, name, _frozen(getattr(self, name)))
        put(self, "changeover", _frozen_nested(self.changeover))

    @classmethod
    def from_step_model(cls, model: StepModel) -> ModelFacts:
        """The facts of a make-to-stock model."""
        ps = model.products
        return cls(
            products=model.product_ids,
            family={p.id: p.family for p in ps},
            abc={p.id: p.abc for p in ps},
            demand_rate={p.id: p.demand_rate for p in ps},
            price={p.id: p.price for p in ps},
            unit_cost={p.id: p.unit_cost for p in ps},
            run_rate={p.id: p.run_rate for p in ps},
            max_storage={p.id: p.max_storage for p in ps},
            holding_cost={p.id: model.holding_cost(p.id) for p in ps},
            lost_sale_cost={p.id: model.lost_sale_cost(p.id) for p in ps},
            changeover={
                "within_family": vars(model.within),
                "across_family": vars(model.across),
            },
            horizon=model.horizon,
            line=model.line_id,
        )


def _cover(stock: Mapping[str, float], rates: Mapping[str, float]) -> dict[str, float]:
    """Days each product's stock lasts; a product with no demand never runs out."""
    out = {}
    for product, units in stock.items():
        rate = rates.get(product, 0.0)
        out[product] = units / rate if rate > 0 else math.inf
    return out


_CHANGEABLE = ("stock", "setup", "period", "actions", "features")


@dataclass(frozen=True)
class PolicyState:
    """What a `decide(state)` file sees (REQ-202). Every field is read-only."""

    products: tuple[str, ...]
    stock: Mapping[str, float]
    days_of_cover: Mapping[str, float]
    demand_rate: Mapping[str, float]
    family: Mapping[str, str]
    abc: Mapping[str, str]
    price: Mapping[str, float]
    unit_cost: Mapping[str, float]
    margin: Mapping[str, float]
    max_storage: Mapping[str, int]
    run_rate: Mapping[str, float]
    holding_cost: Mapping[str, float]
    lost_sale_cost: Mapping[str, float]
    changeover: Mapping[str, Mapping[str, float]]
    setup: str | None
    period: int
    horizon: int
    days_left: int
    actions: tuple[str, ...]
    features: Mapping[str, float]
    _facts: ModelFacts = field(repr=False, compare=False)

    def replace(self, **changes: Any) -> PolicyState:
        """A copy with new `stock`, `setup`, `period`, `actions` or `features`.

        Days of cover and days left are worked out again, so an observation mask that
        hides or rounds stock gives a consistent view (REQ-226).
        """
        unknown = [name for name in changes if name not in _CHANGEABLE]
        if unknown:
            raise ValueError(
                f"PolicyState has no field {unknown[0]!r} you can change; "
                f"change one of: {', '.join(_CHANGEABLE)}"
            )
        values = {name: getattr(self, name) for name in _CHANGEABLE}
        values.update(changes)
        return _build(self._facts, **values)


def _build(
    facts: ModelFacts,
    *,
    stock: Mapping[str, float],
    setup: str | None,
    period: int,
    actions: Iterable[str],
    features: Mapping[str, float],
) -> PolicyState:
    products = facts.products or tuple(stock)
    margin = {p: facts.price.get(p, 0.0) - facts.unit_cost.get(p, 0.0) for p in products}
    return PolicyState(
        products=products,
        stock=_frozen(stock),
        days_of_cover=_frozen(_cover(stock, facts.demand_rate)),
        demand_rate=facts.demand_rate,
        family=facts.family,
        abc=facts.abc,
        price=facts.price,
        unit_cost=facts.unit_cost,
        margin=_frozen(margin),
        max_storage=facts.max_storage,
        run_rate=facts.run_rate,
        holding_cost=facts.holding_cost,
        lost_sale_cost=facts.lost_sale_cost,
        changeover=facts.changeover,
        setup=setup,
        period=period,
        horizon=facts.horizon,
        days_left=facts.horizon - period,
        actions=tuple(actions),
        features=_frozen(features),
        _facts=facts,
    )


def _line_setup(snapshot: Snapshot, line: str | None) -> str | None:
    """The product the line is set up for. With no named line, the only line's setup."""
    if line is not None and line in snapshot.setup:
        return snapshot.setup[line]
    if len(snapshot.setup) == 1:
        return next(iter(snapshot.setup.values()))
    return None


def policy_state(
    snapshot: Snapshot,
    facts: ModelFacts,
    actions: Iterable[str],
    features: Mapping[str, float] | None = None,
) -> PolicyState:
    """The plain-names view of one snapshot (REQ-202)."""
    return _build(
        facts,
        stock=snapshot.stock,
        setup=_line_setup(snapshot, facts.line),
        period=snapshot.period,
        actions=actions,
        features=features or {},
    )


@dataclass(frozen=True)
class DispatchState:
    """What a `DECISION = "dispatch"` file sees at one dispatch decision (COMP-154)."""

    location: str
    queue: tuple[Mapping[str, Any], ...]
    actions: tuple[str, ...]
    time: float
    current: str


def dispatch_state(
    location: str,
    queue: Iterable[Mapping[str, Any]],
    *,
    time: float,
    current: str,
    actions: Iterable[str] = DISPATCH_NAMES,
) -> DispatchState:
    """A dispatch view holding its own read-only copy of each waiting job."""
    return DispatchState(
        location=location,
        queue=tuple(_frozen(job) for job in queue),
        actions=tuple(actions),
        time=float(time),
        current=current,
    )


__all__ = [
    "DispatchState",
    "ModelFacts",
    "PolicyState",
    "dispatch_state",
    "policy_state",
]
