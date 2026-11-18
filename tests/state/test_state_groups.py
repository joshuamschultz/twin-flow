"""T-224: grouped state views (COMP-146, REQ-193, D-138)."""

from __future__ import annotations

import pytest

from twinflow.state.groups import GroupedView, default_views, parse_state_groups
from twinflow.state.records import Snapshot

THINGS = ["GRY-S", "GRY-M", "BLK-S", "NAT-S"]
FAMILIES = {"GRY-S": "grey", "GRY-M": "grey", "BLK-S": "black", "NAT-S": "natural"}
RATES = {"GRY-S": 60.0, "GRY-M": 40.0, "BLK-S": 45.0, "NAT-S": 20.0}
LEVELS = [
    {"name": "out", "below": 1},
    {"name": "low", "below": 4},
    {"name": "ok", "below": 10},
    {"name": "high"},
]


def _snap(stock: dict[str, float], setup: str | None = "GRY-S") -> Snapshot:
    return Snapshot(t=0.0, period=0, stock=stock, setup={"press": setup})


def _view(spec: dict[str, object], name: str = "v") -> GroupedView:
    views, issues = parse_state_groups({"views": {name: spec}}, things=THINGS)
    assert [i for i in issues if i.severity == "error"] == []
    return views[name]


# cover: GRY-S 2d, GRY-M 15d, BLK-S 5d, NAT-S 12d
STOCK = {"GRY-S": 120.0, "GRY-M": 600.0, "BLK-S": 225.0, "NAT-S": 240.0}


def test_levels_by_days_of_cover() -> None:
    view = _view({"stock": {"by": "days_of_cover", "levels": LEVELS}, "setup": "thing"})
    label = view.label(_snap(STOCK), RATES, FAMILIES)
    assert label == "GRY-S:low GRY-M:high BLK-S:ok NAT-S:high | GRY-S"


def test_levels_by_units() -> None:
    levels = [{"name": "few", "below": 200}, {"name": "many"}]
    view = _view({"stock": {"by": "units", "levels": levels, "things": ["GRY-S", "BLK-S"]}})
    assert view.label(_snap(STOCK), RATES, FAMILIES) == "GRY-S:few BLK-S:many"


def test_a_value_on_a_boundary_is_the_next_level() -> None:
    view = _view({"stock": {"by": "units", "levels": [{"name": "a", "below": 10}, {"name": "b"}]}})
    assert view.label(_snap({"GRY-S": 10.0}), RATES, FAMILIES) == "GRY-S:b"


def test_aggregate_family_uses_the_lowest_level_in_the_family() -> None:
    spec = {
        "stock": {"by": "days_of_cover", "aggregate": "family", "levels": LEVELS},
        "setup": "family",
    }
    label = _view(spec).label(_snap(STOCK), RATES, FAMILIES)
    assert label == "grey:low black:ok natural:high | grey"


def test_setup_none_and_idle_line() -> None:
    view = _view({"stock": {"by": "units", "levels": [{"name": "x"}]}, "setup": "none"})
    assert view.label(_snap({"GRY-S": 1.0}), RATES, FAMILIES) == "GRY-S:x"
    on = _view({"setup": "thing"})
    assert on.label(_snap({}, setup=None), RATES, FAMILIES) == "idle"


def test_zero_demand_rate_counts_as_endless_cover() -> None:
    view = _view({"stock": {"by": "days_of_cover", "levels": LEVELS}})
    assert view.label(_snap({"GRY-S": 0.0}), {"GRY-S": 0.0}, FAMILIES) == "GRY-S:high"


def test_default_views_exist_without_config() -> None:
    views, issues = parse_state_groups(None, things=THINGS)
    assert issues == []
    assert set(views) == {"full", "family"}
    assert set(default_views()) == {"full", "family"}

    family = views["family"].label(_snap(STOCK), RATES, FAMILIES)
    assert family == "grey:low black:ok natural:high | grey"
    full = views["full"].label(_snap({"GRY-S": 120.0, "BLK-S": 2.5}), RATES, FAMILIES)
    assert full == "GRY-S:120 BLK-S:2.5 | GRY-S"


def test_user_views_sit_beside_the_defaults() -> None:
    views, _ = parse_state_groups(
        {"views": {"simple": {"stock": {"by": "units", "levels": [{"name": "x"}]}}}},
        things=THINGS,
    )
    assert set(views) == {"full", "family", "simple"}


def test_labels_join_several_lines_in_name_order() -> None:
    snap = Snapshot(t=0.0, period=0, setup={"b": "BLK-S", "a": "GRY-S"})
    assert _view({"setup": "family"}).label(snap, RATES, FAMILIES) == "grey,black"


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ("views", "state_groups"),
        ({"views": []}, "state_groups.views"),
        ({"views": {"v": 3}}, "state_groups.views.v"),
        ({"extra": {}}, "state_groups.extra"),
        ({"views": {"full": {"setup": "thing"}}}, "state_groups.views.full"),
        ({"views": {"v": {"setup": "machine"}}}, "state_groups.views.v.setup"),
        ({"views": {"v": {"colour": 1}}}, "state_groups.views.v.colour"),
        (
            {"views": {"v": {"stock": {"by": "weight", "levels": LEVELS}}}},
            "state_groups.views.v.stock.by",
        ),
        ({"views": {"v": {"stock": {"by": "units"}}}}, "state_groups.views.v.stock.levels"),
        (
            {"views": {"v": {"stock": {"by": "units", "levels": LEVELS, "aggregate": "sum"}}}},
            "state_groups.views.v.stock.aggregate",
        ),
        (
            {
                "views": {
                    "v": {
                        "stock": {
                            "by": "units",
                            "levels": [
                                {"name": "a", "below": 5},
                                {"name": "b", "below": 2},
                                {"name": "c"},
                            ],
                        }
                    }
                }
            },
            "state_groups.views.v.stock.levels[1].below",
        ),
        (
            {
                "views": {
                    "v": {
                        "stock": {
                            "by": "units",
                            "levels": [{"name": "a", "below": 5}, {"name": "b", "below": 9}],
                        }
                    }
                }
            },
            "state_groups.views.v.stock.levels[1].below",
        ),
        (
            {"views": {"v": {"stock": {"by": "units", "levels": [{"name": "a"}, {"name": "b"}]}}}},
            "state_groups.views.v.stock.levels[0].below",
        ),
        (
            {
                "views": {
                    "v": {
                        "stock": {
                            "by": "units",
                            "levels": [{"name": "a", "below": 5}, {"name": "a"}],
                        }
                    }
                }
            },
            "state_groups.views.v.stock.levels[1].name",
        ),
        (
            {
                "views": {
                    "v": {"stock": {"by": "units", "levels": LEVELS, "things": ["GRY-S", "PNK-S"]}}
                }
            },
            "state_groups.views.v.stock.things[1]",
        ),
    ],
)
def test_bad_config_gives_named_errors(raw: object, path: str) -> None:
    views, issues = parse_state_groups(raw, things=THINGS)
    errors = [i for i in issues if i.severity == "error"]
    assert [e.path for e in errors] == [path]
    assert errors[0].message and errors[0].suggestion
    assert {"full", "family"} <= set(views)


def test_unknown_thing_error_suggests_a_close_name() -> None:
    _, issues = parse_state_groups(
        {"views": {"v": {"stock": {"by": "units", "levels": LEVELS, "things": ["GRY-SS"]}}}},
        things=THINGS,
    )
    assert "GRY-S" in issues[0].suggestion
