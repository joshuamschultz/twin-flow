"""COMP-145: the optional `economics:` model section (REQ-192).

```yaml
economics:
  things:   {<thing>: {price, unit_cost, holding_per_unit_day, lost_sale_cost, late_cost_per_day}}
  defaults: {holding_rate, lost_sale_rate}   # holding = rate x unit_cost, lost sale = rate x price
  setup:    {cost, within_family, across_family}
```

A twin should run before finance gives numbers (D-133): a missing value costs 0 and adds
one info issue per field kind (never one per thing). A bad value is an error naming its
path and also costs 0; parsing never raises.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from twinflow.state.issues import Issue, did_you_mean, number_or_issue, unknown_keys

THING_FIELDS: tuple[str, ...] = (
    "price",
    "unit_cost",
    "holding_per_unit_day",
    "lost_sale_cost",
    "late_cost_per_day",
)
DEFAULT_FIELDS: tuple[str, ...] = ("holding_rate", "lost_sale_rate")
SETUP_FIELDS: tuple[str, ...] = ("cost", "within_family", "across_family")
SECTIONS: tuple[str, ...] = ("things", "defaults", "setup")

# What a missing field means, in plain words, for its info issue.
_MISSING_MEANS = {
    "price": "sales earn $0",
    "unit_cost": "making costs $0",
    "holding_per_unit_day": "keeping stock costs $0",
    "lost_sale_cost": "a lost sale costs $0",
    "late_cost_per_day": "late orders cost $0",
}


@dataclass(frozen=True)
class ThingEconomics:
    """Money facts for one thing (product or part). All costs are positive dollars."""

    price: float = 0.0
    unit_cost: float = 0.0
    holding_per_unit_day: float = 0.0
    lost_sale_cost: float = 0.0
    late_cost_per_day: float = 0.0


@dataclass(frozen=True)
class SetupEconomics:
    """Dollars per setup change; within/across family fall back to the flat `cost`."""

    cost: float = 0.0
    within_family: float = 0.0
    across_family: float = 0.0


_NO_THINGS: Mapping[str, ThingEconomics] = MappingProxyType({})


@dataclass(frozen=True)
class Economics:
    """The parsed section. `thing(id)` gives all-zero money for a thing it does not know."""

    things: Mapping[str, ThingEconomics] = field(default=_NO_THINGS)
    setup: SetupEconomics = field(default_factory=SetupEconomics)

    def thing(self, thing_id: str) -> ThingEconomics:
        return self.things.get(thing_id, _ZERO)


_ZERO = ThingEconomics()


def _numbers(
    raw: Mapping[object, object], names: Sequence[str], path: str, issues: list[Issue]
) -> dict[str, float]:
    """The valid numbers among `names`; bad ones become errors (and are left out)."""
    issues.extend(unknown_keys(dict(raw), names, path))
    found: dict[str, float] = {}
    for name in names:
        if name in raw:
            number, issue = number_or_issue(raw[name], f"{path}.{name}")
            if issue is not None:
                issues.append(issue)
            elif number is not None:
                found[name] = number
    return found


def _mapping(raw: object, path: str, issues: list[Issue]) -> Mapping[object, object]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        issues.append(
            Issue(path, "This must be a set of named fields.", "error", "See the economics how-to.")
        )
        return {}
    return raw


def _thing(values: dict[str, float], defaults: dict[str, float]) -> dict[str, float]:
    """Fill holding and lost-sale cost from the default rates when not given."""
    out = dict(values)
    if "holding_per_unit_day" not in out and "holding_rate" in defaults and "unit_cost" in out:
        out["holding_per_unit_day"] = defaults["holding_rate"] * out["unit_cost"]
    if "lost_sale_cost" not in out and "lost_sale_rate" in defaults and "price" in out:
        out["lost_sale_cost"] = defaults["lost_sale_rate"] * out["price"]
    return out


def _missing_issue(name: str, missing: list[str], total: int, path: str) -> Issue:
    where = f"{path}.things.{missing[0]}.{name}" if len(missing) == 1 else f"{path}.things.*.{name}"
    who = "every thing" if len(missing) == total and total > 1 else ", ".join(missing)
    return Issue(
        where,
        f"No {name} for {who}, so {_MISSING_MEANS[name]}.",
        "info",
        f"Add {name} under {path}.things.<thing> to count this money.",
    )


def parse_economics(
    raw: object, things: Sequence[str] = (), path: str = "economics"
) -> tuple[Economics, list[Issue]]:
    """Parse the section. `things` are the model's thing ids (empty: accept any id)."""
    if raw is None:
        info = Issue(
            path,
            "No economics section, so all money is $0.",
            "info",
            "Add an economics section to count money.",
        )
        return Economics(), [info]
    issues: list[Issue] = []
    section = _mapping(raw, path, issues)
    if issues:
        return Economics(), issues
    issues.extend(unknown_keys(dict(section), SECTIONS, path))
    defaults = _numbers(
        _mapping(section.get("defaults"), f"{path}.defaults", issues),
        DEFAULT_FIELDS,
        f"{path}.defaults",
        issues,
    )
    setup_raw = _numbers(
        _mapping(section.get("setup"), f"{path}.setup", issues),
        SETUP_FIELDS,
        f"{path}.setup",
        issues,
    )

    parsed: dict[str, dict[str, float]] = {}
    for key, value in _mapping(section.get("things"), f"{path}.things", issues).items():
        thing_path = f"{path}.things.{key}"
        if things and key not in things:
            issues.append(
                Issue(
                    thing_path,
                    f"'{key}' is not a thing in this model.",
                    "error",
                    did_you_mean(str(key), things),
                )
            )
            continue
        values = _numbers(_mapping(value, thing_path, issues), THING_FIELDS, thing_path, issues)
        parsed[str(key)] = _thing(values, defaults)

    every = list(dict.fromkeys([*things, *parsed]))
    for name in THING_FIELDS:
        missing = [t for t in every if name not in parsed.get(t, {})]
        if missing:
            issues.append(_missing_issue(name, missing, len(every), path))
    if not setup_raw:
        issues.append(
            Issue(
                f"{path}.setup.cost",
                "No setup cost, so setup changes cost $0.",
                "info",
                f"Add {path}.setup.cost.",
            )
        )

    flat = setup_raw.get("cost", 0.0)
    setup = SetupEconomics(
        cost=flat,
        within_family=setup_raw.get("within_family", flat),
        across_family=setup_raw.get("across_family", flat),
    )
    things_out = {t: ThingEconomics(**parsed.get(t, {})) for t in every}
    return Economics(MappingProxyType(things_out), setup), issues
