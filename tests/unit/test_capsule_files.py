"""A capsule may point at input files that sit next to it (TF-006 business showcases).

A planner keeps the product table, the order book and the purchase orders in their own
files. `ScenarioCapsule.load(path)` reads them:

- `model: shop.yaml` reads the whole model from a YAML file;
- a list inside `model` or `snapshot` may be the name of a CSV file. Each row is one item.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.scenario import CapsuleValidationError, ScenarioCapsule

HEADER = """schema_version: '0.1'
required_capabilities: [make_to_stock.step]
snapshot: {id: s, as_of: '2026-11-17T08:00:00-06:00', model_revision: '1'}
experiment: {id: e, replications: 1, seed: 1}
assumptions: []
provenance: {}
"""

MODEL = """model:
  domain: make_to_stock
  horizon: 10
  line: {id: press, changeover: {within_family: {cost: 1, output_lost: 0.1},
                                 across_family: {cost: 2, output_lost: 0.5}}}
  products: data/products.csv
"""

PRODUCTS = """id,family,abc,demand,run_rate,price,unit_cost,max_storage
GRY-S,grey,A,60,450,12.0,7.2,1500
0042,black,,45,450,12.6,7.56,1125
"""


def _write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_list_can_be_a_csv_file_next_to_the_capsule(tmp_path: Path) -> None:
    capsule_file = _write(tmp_path, "plant.twin.yaml", HEADER + MODEL)
    _write(tmp_path, "data/products.csv", PRODUCTS)

    capsule = ScenarioCapsule.load(capsule_file)

    first, second = capsule.model["products"]
    assert first == {
        "id": "GRY-S",
        "family": "grey",
        "abc": "A",
        "demand": 60,
        "run_rate": 450,
        "price": 12.0,
        "unit_cost": 7.2,
        "max_storage": 1500,
    }
    # An id stays text (leading zeros kept); a blank cell leaves the field out.
    assert second["id"] == "0042"
    assert "abc" not in second
    assert capsule.validate() == []


def test_the_model_can_be_a_yaml_file_next_to_the_capsule(tmp_path: Path) -> None:
    model = MODEL.replace("model:\n", "").replace("\n  ", "\n").lstrip()
    _write(tmp_path, "plant.yaml", model)
    _write(tmp_path, "data/products.csv", PRODUCTS)
    capsule_file = _write(tmp_path, "plant.twin.yaml", HEADER + "model: plant.yaml\n")

    capsule = ScenarioCapsule.load(capsule_file)

    assert capsule.model["domain"] == "make_to_stock"
    assert len(capsule.model["products"]) == 2


def test_a_dotted_header_makes_a_nested_field(tmp_path: Path) -> None:
    text = HEADER.replace("snapshot: {", "snapshot: {orders: orders.csv, ") + MODEL
    _write(tmp_path, "data/products.csv", PRODUCTS)
    _write(tmp_path, "orders.csv", "id,risk.minimum_days,risk.group\nA-1,1,port\n")
    capsule = ScenarioCapsule.load(_write(tmp_path, "plant.twin.yaml", text))

    assert capsule.snapshot["orders"] == [
        {"id": "A-1", "risk": {"minimum_days": 1, "group": "port"}}
    ]


def test_a_missing_file_is_named_in_plain_words(tmp_path: Path) -> None:
    capsule_file = _write(tmp_path, "plant.twin.yaml", HEADER + MODEL)

    with pytest.raises(CapsuleValidationError) as caught:
        ScenarioCapsule.load(capsule_file)

    issue = caught.value.issues[0]
    assert issue.path == "$.model.products"
    assert "data/products.csv" in issue.message
    assert "no such file" in issue.message


def test_a_file_outside_the_capsule_folder_is_refused(tmp_path: Path) -> None:
    inner = tmp_path / "plant"
    _write(tmp_path, "products.csv", PRODUCTS)
    text = HEADER + MODEL.replace("data/products.csv", "../products.csv")
    capsule_file = _write(inner, "plant.twin.yaml", text)

    with pytest.raises(CapsuleValidationError) as caught:
        ScenarioCapsule.load(capsule_file)

    assert "inside the capsule's folder" in caught.value.issues[0].message


def test_a_row_with_too_many_cells_names_the_row(tmp_path: Path) -> None:
    capsule_file = _write(tmp_path, "plant.twin.yaml", HEADER + MODEL)
    _write(tmp_path, "data/products.csv", PRODUCTS + "BAD,grey,A,1,2,3,4,5,6\n")

    with pytest.raises(CapsuleValidationError) as caught:
        ScenarioCapsule.load(capsule_file)

    message = caught.value.issues[0].message
    assert "row 4" in message and "more cells than the header" in message


def test_text_without_a_folder_keeps_the_file_name(tmp_path: Path) -> None:
    capsule = ScenarioCapsule.loads(HEADER + MODEL)
    assert capsule.model["products"] == "data/products.csv"
