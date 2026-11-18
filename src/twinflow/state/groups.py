"""COMP-146: grouped views of a snapshot, for Markov maps and money leaks (REQ-193).

```yaml
state_groups:
  views:
    simple:
      stock:
        by: days_of_cover      # or units
        aggregate: family      # or none
        things: all            # or a list of thing ids
        levels: [{name: out, below: 1}, {name: low, below: 4}, {name: ok, below: 10}, {name: high}]
      setup: family          # none | thing | family
```

A level holds values below its `below` and at or above the level before it; the last level
has no `below` and holds the rest. Labels are short and readable, for example
`grey:low black:ok natural:high | grey`. The views `full` (exact state) and `family`
(lowest-cover level per family plus the family on the line) always exist (D-138).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from twinflow.state.issues import Issue, did_you_mean, number_or_issue, unknown_keys
from twinflow.state.records import Snapshot

StockBy = Literal["days_of_cover", "units"]
Aggregate = Literal["none", "family"]
SetupBy = Literal["none", "thing", "family"]

STOCK_BY: tuple[str, ...] = ("days_of_cover", "units")
AGGREGATES: tuple[str, ...] = ("none", "family")
SETUP_BY: tuple[str, ...] = ("none", "thing", "family")
BUILT_IN_VIEWS: tuple[str, ...] = ("full", "family")
IDLE = "idle"

_LEVELS_FIX = (
    "List levels from low to high; each has a name, "
    "and every level but the last has a 'below' number."
)


@dataclass(frozen=True)
class Level:
    """A named band of values below `below` (`None` on the last level: everything else)."""

    name: str
    below: float | None = None


@dataclass(frozen=True)
class StockGroups:
    """How a view groups stock: per thing, or the lowest level per family."""

    by: StockBy
    levels: tuple[Level, ...]
    things: tuple[str, ...] | None = None  # None: every thing in the snapshot
    aggregate: Aggregate = "none"

    def level_index(self, value: float) -> int:
        for index, level in enumerate(self.levels):
            if level.below is None or value < level.below:
                return index
        return len(self.levels) - 1


@dataclass(frozen=True)
class GroupedView:
    """A named way to turn a snapshot into a short label (REQ-193)."""

    name: str
    stock: StockGroups | None = None
    setup: SetupBy = "none"
    exact: bool = False  # the `full` view: exact stock numbers

    def label(
        self,
        snapshot: Snapshot,
        demand_rate: Mapping[str, float],
        families: Mapping[str, str],
    ) -> str:
        """The view's label for one snapshot; a thing with no demand has endless cover."""
        parts = [
            self._stock_part(snapshot, demand_rate, families),
            self._setup_part(snapshot, families),
        ]
        return " | ".join(part for part in parts if part)

    def _stock_part(
        self, snapshot: Snapshot, demand_rate: Mapping[str, float], families: Mapping[str, str]
    ) -> str:
        if self.exact:
            return " ".join(f"{thing}:{_number(units)}" for thing, units in snapshot.stock.items())
        groups = self.stock
        if groups is None:
            return ""
        things = groups.things if groups.things is not None else tuple(snapshot.stock)
        stock = snapshot.stock
        levels: dict[str, int] = {}
        for thing in things:
            units = stock.get(thing, 0.0)
            if groups.by == "days_of_cover":
                rate = demand_rate.get(thing, 0.0)
                value = units / rate if rate > 0 else math.inf
            else:
                value = units
            index = groups.level_index(value)
            key = families.get(thing, thing) if groups.aggregate == "family" else thing
            levels[key] = min(index, levels.get(key, index))
        names = groups.levels
        return " ".join(f"{key}:{names[index].name}" for key, index in levels.items())

    def _setup_part(self, snapshot: Snapshot, families: Mapping[str, str]) -> str:
        if self.setup == "none" or not snapshot.setup:
            return ""
        shown = []
        for line in sorted(snapshot.setup):
            thing = snapshot.setup[line]
            if thing is None:
                shown.append(IDLE)
            else:
                shown.append(families.get(thing, thing) if self.setup == "family" else thing)
        return ",".join(shown)


def _number(value: float) -> str:
    return str(int(value)) if value.is_integer() else repr(value)


def default_views() -> dict[str, GroupedView]:
    """The views that always exist (D-138): `full` and `family` (low < 3 <= ok < 10 <= high
    days of cover, the same bands as the RL repo's family view)."""
    family = StockGroups(
        by="days_of_cover",
        levels=(Level("low", 3.0), Level("ok", 10.0), Level("high")),
        aggregate="family",
    )
    return {
        "full": GroupedView("full", setup="thing", exact=True),
        "family": GroupedView("family", stock=family, setup="family"),
    }


def _error(path: str, message: str, suggestion: str) -> Issue:
    return Issue(path, message, "error", suggestion)


def _choice(
    raw: Mapping[object, object],
    key: str,
    choices: Sequence[str],
    default: str,
    path: str,
    issues: list[Issue],
) -> str:
    value = raw.get(key, default)
    if value not in choices:
        issues.append(
            _error(
                f"{path}.{key}",
                f"{value!r} is not a valid {key}.",
                did_you_mean(str(value), choices),
            )
        )
        return default
    return cast(str, value)


def _levels(raw: object, path: str, issues: list[Issue]) -> tuple[Level, ...] | None:
    if not isinstance(raw, list) or not raw:
        issues.append(_error(path, "A stock group needs a list of levels.", _LEVELS_FIX))
        return None
    levels: list[Level] = []
    start = len(issues)
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"{path}[{index}]"
        last = index == len(raw) - 1
        if not isinstance(item, Mapping):
            issues.append(_error(where, "Each level must have a name.", _LEVELS_FIX))
            continue
        issues.extend(unknown_keys(dict(item), ("name", "below"), where))
        name = item.get("name")
        if not isinstance(name, str) or not name:
            issues.append(
                _error(
                    f"{where}.name", "Each level must have a name.", "Write a short word, like low."
                )
            )
            continue
        if name in seen:
            issues.append(
                _error(
                    f"{where}.name",
                    f"The level name '{name}' is used twice.",
                    "Give every level its own name.",
                )
            )
            continue
        seen.add(name)
        below = _below(item, where, last, levels, issues)
        levels.append(Level(name, below))
    return tuple(levels) if len(issues) == start else None


def _below(
    item: Mapping[object, object], where: str, last: bool, levels: list[Level], issues: list[Issue]
) -> float | None:
    if last:
        if "below" in item:
            issues.append(
                _error(
                    f"{where}.below",
                    "The last level holds everything else, so it has no 'below'.",
                    "Remove 'below' from the last level.",
                )
            )
        return None
    if "below" not in item:
        issues.append(
            _error(
                f"{where}.below", "Every level but the last needs a 'below' number.", _LEVELS_FIX
            )
        )
        return None
    below, issue = number_or_issue(item["below"], f"{where}.below")
    if issue is not None:
        issues.append(issue)
        return None
    previous = levels[-1].below if levels else None
    if below is not None and previous is not None and below <= previous:
        issues.append(
            _error(
                f"{where}.below",
                f"{below:g} is not above the level before it ({previous:g}).",
                _LEVELS_FIX,
            )
        )
        return None
    return below


def _things(
    raw: object, path: str, known: Sequence[str], issues: list[Issue]
) -> tuple[str, ...] | None:
    if raw is None or raw == "all":
        return None
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
        issues.append(
            _error(
                path,
                "things must be 'all' or a list of thing ids.",
                "Write things: all, or a list like [GRY-S, BLK-S].",
            )
        )
        return None
    for index, thing in enumerate(raw):
        if known and thing not in known:
            issues.append(
                _error(
                    f"{path}[{index}]",
                    f"'{thing}' is not a thing in this model.",
                    did_you_mean(thing, known),
                )
            )
    return tuple(raw)


def _stock(raw: object, path: str, known: Sequence[str], issues: list[Issue]) -> StockGroups | None:
    if not isinstance(raw, Mapping):
        issues.append(
            _error(
                path,
                "stock must be a set of named fields.",
                "Give stock a 'by' and a list of levels.",
            )
        )
        return None
    issues.extend(unknown_keys(dict(raw), ("by", "levels", "things", "aggregate"), path))
    start = len(issues)
    by = _choice(raw, "by", STOCK_BY, "days_of_cover", path, issues)
    aggregate = _choice(raw, "aggregate", AGGREGATES, "none", path, issues)
    things = _things(raw.get("things"), f"{path}.things", known, issues)
    levels = _levels(raw.get("levels"), f"{path}.levels", issues)
    if levels is None or len(issues) != start:
        return None
    return StockGroups(cast(StockBy, by), levels, things, cast(Aggregate, aggregate))


def _view(
    name: str, raw: object, path: str, known: Sequence[str], issues: list[Issue]
) -> GroupedView | None:
    if name in BUILT_IN_VIEWS:
        issues.append(
            _error(path, f"The view name '{name}' is built in.", "Pick another name for your view.")
        )
        return None
    if not isinstance(raw, Mapping):
        issues.append(
            _error(
                path, "A view must be a set of named fields.", "Give the view stock and/or setup."
            )
        )
        return None
    start = len(issues)
    issues.extend(unknown_keys(dict(raw), ("stock", "setup"), path))
    setup = _choice(raw, "setup", SETUP_BY, "none", path, issues)
    stock = _stock(raw["stock"], f"{path}.stock", known, issues) if "stock" in raw else None
    if len(issues) != start:
        return None
    return GroupedView(name, stock, cast(SetupBy, setup))


def parse_state_groups(
    raw: object, things: Sequence[str] = (), path: str = "state_groups"
) -> tuple[dict[str, GroupedView], list[Issue]]:
    """The built-in views plus every valid user view; a bad view is reported and left out."""
    views = default_views()
    issues: list[Issue] = []
    if raw is None:
        return views, issues
    if not isinstance(raw, Mapping):
        issues.append(
            _error(
                path,
                "state_groups must be a set of named fields.",
                "Put your views under state_groups.views.",
            )
        )
        return views, issues
    issues.extend(unknown_keys(dict(raw), ("views",), path))
    user = raw.get("views", {})
    if not isinstance(user, Mapping):
        issues.append(
            _error(
                f"{path}.views",
                "views must map a view name to its settings.",
                "Write views: {simple: {...}}.",
            )
        )
        return views, issues
    for name, spec in user.items():
        view = _view(str(name), spec, f"{path}.views.{name}", things, issues)
        if view is not None:
            views[view.name] = view
    return views, issues
