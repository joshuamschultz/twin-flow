"""T-228: parse and validate a `domain: make_to_stock` model (COMP-150, REQ-195, REQ-213)."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from twinflow.engines.step.model import StepModel, parse_step_model


def _raw() -> dict[str, Any]:
    return {
        "domain": "make_to_stock",
        "period": "day",
        "horizon": 60,
        "start_cover_days": 8,
        "line": {
            "id": "press",
            "changeover": {
                "within_family": {"cost": 200, "output_lost": 0.10},
                "across_family": {"cost": 1200, "output_lost": 0.50},
            },
        },
        "products": [
            {
                "id": "GRY-S",
                "family": "grey",
                "abc": "A",
                "demand": 60,
                "run_rate": 450,
                "price": 12.0,
                "unit_cost": 7.2,
                "max_storage": 1500,
            },
            {
                "id": "BLK-S",
                "family": "black",
                "abc": "A",
                "demand": 45,
                "run_rate": 450,
                "price": 12.6,
                "unit_cost": 7.56,
                "max_storage": 1125,
            },
        ],
        "economics": {"defaults": {"holding_rate": 0.01, "lost_sale_rate": 0.30}},
    }


def _errors(raw: dict[str, Any]) -> list[Any]:
    model, issues = parse_step_model(raw)
    errors = [i for i in issues if i.severity == "error"]
    assert model is None if errors else model is not None
    return errors


def test_valid_model_parses_in_product_order() -> None:
    model, issues = parse_step_model(_raw())
    assert isinstance(model, StepModel)
    assert [i for i in issues if i.severity == "error"] == []
    assert model.line_id == "press"
    assert model.horizon == 60
    assert model.start_cover_days == 8.0
    assert model.product_ids == ("GRY-S", "BLK-S")
    assert model.families == ("grey", "black")
    grey = model.products[0]
    assert (grey.demand_rate, grey.run_rate, grey.max_storage) == (60.0, 450.0, 1500)
    assert (grey.price, grey.unit_cost, grey.abc) == (12.0, 7.2, "A")
    assert model.within.cost == 200 and model.within.output_lost == 0.10
    assert model.across.cost == 1200 and model.across.output_lost == 0.50


def test_money_comes_from_economics_defaults() -> None:
    model, _ = parse_step_model(_raw())
    assert model is not None
    assert model.holding_cost("GRY-S") == 0.01 * 7.2
    assert model.lost_sale_cost("BLK-S") == 0.30 * 12.6


def test_missing_defaults_cost_zero_with_info_issue() -> None:
    raw = _raw()
    del raw["economics"]
    model, issues = parse_step_model(raw)
    assert model is not None
    assert model.holding_cost("GRY-S") == 0.0
    assert any(i.severity == "info" for i in issues)


def test_more_than_one_line_is_refused_in_plain_words() -> None:
    raw = _raw()
    raw["line"] = [raw["line"], copy.deepcopy(raw["line"])]
    errors = _errors(raw)
    assert errors[0].path == "model.line"
    assert "one line" in errors[0].message


def test_lines_key_is_refused() -> None:
    raw = _raw()
    raw["lines"] = [raw.pop("line")]
    assert any("one line" in e.message for e in _errors(raw))


def test_duplicate_product_ids_are_named() -> None:
    raw = _raw()
    raw["products"][1]["id"] = "GRY-S"
    errors = _errors(raw)
    assert errors[0].path == "model.products[1].id"
    assert "GRY-S" in errors[0].message and "twice" in errors[0].message


def test_unknown_family_is_named_with_the_valid_ones() -> None:
    raw = _raw()
    raw["families"] = ["grey", "black"]
    raw["products"][1]["family"] = "blak"
    errors = _errors(raw)
    assert errors[0].path == "model.products[1].family"
    assert "blak" in errors[0].message and "black" in errors[0].suggestion


def test_missing_family_is_an_error() -> None:
    raw = _raw()
    del raw["products"][0]["family"]
    assert _errors(raw)[0].path == "model.products[0].family"


@pytest.mark.parametrize("field", ["demand", "run_rate", "max_storage"])
@pytest.mark.parametrize("bad", [0, -5, "many"])
def test_non_positive_rates_are_errors(field: str, bad: object) -> None:
    raw = _raw()
    raw["products"][0][field] = bad
    errors = _errors(raw)
    assert errors[0].path == f"model.products[0].{field}"
    assert errors[0].suggestion


@pytest.mark.parametrize("bad", [-0.1, 1.5, "half"])
def test_output_lost_outside_zero_one_is_an_error(bad: object) -> None:
    raw = _raw()
    raw["line"]["changeover"]["across_family"]["output_lost"] = bad
    errors = _errors(raw)
    assert errors[0].path == "model.line.changeover.across_family.output_lost"


def test_negative_changeover_cost_is_an_error() -> None:
    raw = _raw()
    raw["line"]["changeover"]["within_family"]["cost"] = -1
    assert _errors(raw)[0].path == "model.line.changeover.within_family.cost"


@pytest.mark.parametrize("field", ["horizon", "start_cover_days"])
def test_horizon_and_cover_are_checked(field: str) -> None:
    raw = _raw()
    raw[field] = -3
    assert _errors(raw)[0].path == f"model.{field}"


def test_period_must_be_day() -> None:
    raw = _raw()
    raw["period"] = "week"
    assert _errors(raw)[0].path == "model.period"


def test_no_products_is_an_error() -> None:
    raw = _raw()
    raw["products"] = []
    assert _errors(raw)[0].path == "model.products"


def test_unknown_product_field_gets_a_hint() -> None:
    raw = _raw()
    raw["products"][0]["demnd"] = 3
    errors = _errors(raw)
    assert "demand" in errors[0].suggestion


def test_unknown_economics_product_is_an_error() -> None:
    raw = _raw()
    raw["economics"]["things"] = {"NOPE": {"price": 1}}
    assert any(e.path == "model.economics.things.NOPE" for e in _errors(raw))


def test_state_groups_errors_come_through() -> None:
    raw = _raw()
    raw["state_groups"] = {"views": {"full": {"setup": "family"}}}
    assert any(e.path.startswith("model.state_groups") for e in _errors(raw))


def test_non_mapping_model_is_an_error() -> None:
    model, issues = parse_step_model(["nope"])  # type: ignore[arg-type]
    assert model is None and issues[0].severity == "error"
