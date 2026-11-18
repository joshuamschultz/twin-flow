"""T-223: the optional `economics:` section (COMP-145, REQ-192, D-133)."""

from __future__ import annotations

import pytest

from twinflow.state.economics import Economics, ThingEconomics, parse_economics
from twinflow.state.issues import Issue

FULL = {
    "things": {
        "GRY-S": {
            "price": 12.0,
            "unit_cost": 7.2,
            "holding_per_unit_day": 0.05,
            "lost_sale_cost": 4.0,
            "late_cost_per_day": 1.5,
        },
        "BLK-S": {
            "price": 12.6,
            "unit_cost": 7.56,
            "holding_per_unit_day": 0.06,
            "lost_sale_cost": 3.5,
            "late_cost_per_day": 2.0,
        },
    },
    "setup": {"cost": 300.0, "within_family": 200.0, "across_family": 1200.0},
}


def _errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def _infos(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "info"]


def test_full_section_parses_with_no_issues() -> None:
    economics, issues = parse_economics(FULL, things=["GRY-S", "BLK-S"])
    assert issues == []
    assert economics.thing("GRY-S") == ThingEconomics(
        price=12.0,
        unit_cost=7.2,
        holding_per_unit_day=0.05,
        lost_sale_cost=4.0,
        late_cost_per_day=1.5,
    )
    assert economics.setup.cost == 300.0
    assert economics.setup.within_family == 200.0
    assert economics.setup.across_family == 1200.0


def test_rates_derive_holding_and_lost_sale_cost() -> None:
    raw = {
        "things": {"A": {"price": 10.0, "unit_cost": 6.0, "late_cost_per_day": 0.0}},
        "defaults": {"holding_rate": 0.01, "lost_sale_rate": 0.30},
        "setup": {"cost": 0.0},
    }
    economics, issues = parse_economics(raw, things=["A"])
    assert issues == []
    assert economics.thing("A").holding_per_unit_day == pytest.approx(0.06)
    assert economics.thing("A").lost_sale_cost == pytest.approx(3.0)


def test_explicit_cost_wins_over_a_rate() -> None:
    raw = {
        "things": {"A": {"price": 10.0, "unit_cost": 6.0, "holding_per_unit_day": 1.0}},
        "defaults": {"holding_rate": 0.01},
    }
    economics, _ = parse_economics(raw, things=["A"])
    assert economics.thing("A").holding_per_unit_day == 1.0


def test_within_and_across_family_fall_back_to_the_flat_cost() -> None:
    economics, _ = parse_economics({"setup": {"cost": 50.0}})
    assert economics.setup.within_family == 50.0
    assert economics.setup.across_family == 50.0


def test_missing_fields_cost_zero_with_one_info_per_field_kind() -> None:
    raw = {"things": {"A": {"price": 10.0}, "B": {"price": 11.0}}}
    economics, issues = parse_economics(raw, things=["A", "B", "C"])

    assert _errors(issues) == []
    assert economics.thing("C") == ThingEconomics()
    assert economics.thing("A").unit_cost == 0.0
    infos = _infos(issues)
    kinds = [i.path for i in infos]
    # one issue per field kind, never one per thing
    assert len(infos) == len(set(kinds)) == 6
    assert "economics.things.C.price" in kinds
    assert "economics.things.*.unit_cost" in kinds
    assert "economics.setup.cost" in kinds
    for info in infos:
        assert info.message and info.suggestion
    price = next(i for i in infos if i.path.endswith(".price"))
    assert "C" in price.message


def test_absent_section_is_all_zero_with_one_info() -> None:
    economics, issues = parse_economics(None, things=["A"])
    assert economics == Economics()
    assert economics.thing("A").price == 0.0
    assert [(i.path, i.severity) for i in issues] == [("economics", "info")]


@pytest.mark.parametrize(
    ("raw", "path"),
    [
        ({"things": {"A": {"price": -1.0}}}, "economics.things.A.price"),
        ({"things": {"A": {"unit_cost": "cheap"}}}, "economics.things.A.unit_cost"),
        ({"things": {"A": {"price": True}}}, "economics.things.A.price"),
        ({"things": {"A": {"price": float("nan")}}}, "economics.things.A.price"),
        ({"defaults": {"holding_rate": -0.1}}, "economics.defaults.holding_rate"),
        ({"setup": {"across_family": "lots"}}, "economics.setup.across_family"),
        ({"things": {"A": {"prise": 1.0}}}, "economics.things.A.prise"),
        ({"bogus": 1}, "economics.bogus"),
        ({"things": ["A"]}, "economics.things"),
        ({"things": {"Z": {"price": 1.0}}}, "economics.things.Z"),
        ("money", "economics"),
    ],
)
def test_bad_values_are_errors_naming_the_path(raw: object, path: str) -> None:
    economics, issues = parse_economics(raw, things=["A"])
    errors = _errors(issues)
    assert [e.path for e in errors] == [path]
    assert errors[0].message
    assert isinstance(economics, Economics)


def test_a_bad_value_costs_zero() -> None:
    economics, _ = parse_economics({"things": {"A": {"price": -1.0}}}, things=["A"])
    assert economics.thing("A").price == 0.0


def test_unknown_things_are_allowed_when_no_model_things_are_given() -> None:
    economics, issues = parse_economics({"things": {"Z": {"price": 1.0}}})
    assert _errors(issues) == []
    assert economics.thing("Z").price == 1.0
