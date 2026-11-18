"""`twinflow scenario run --summary` prints the answer in plain lines (TF-006 showcases).

It also reads capsules that point at input files next to them (`data/products.csv`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from twinflow.cli.main import main
from twinflow.cli.run_summary import summary_lines

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "examples" / "molding-press" / "molding-press.twin.yaml"
SHOP = ROOT / "examples" / "cnc-shop" / "cnc-shop.twin.yaml"


def _run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["scenario", "run", *args])
    return code, capsys.readouterr().out


def test_scenario_run_reads_a_product_table_next_to_the_capsule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = yaml.safe_load(PRESS.read_text(encoding="utf-8"))
    products = data["model"]["products"]
    columns = list(products[0])
    lines = [",".join(columns)] + [",".join(str(p[c]) for c in columns) for p in products]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "products.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    data["model"]["products"] = "data/products.csv"
    capsule = tmp_path / "press.twin.yaml"
    capsule.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "run"
    code, printed = _run([str(capsule), "--reps", "2", "--seed", "2024", "--out", str(out)], capsys)

    assert code == 0
    assert json.loads(printed)["metrics"]["mean_profit"] > 0


def test_summary_for_a_make_to_stock_plant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "run"
    code, printed = _run([str(PRESS), "--reps", "2", "--out", str(out), "--summary"], capsys)

    assert code == 0
    assert printed.startswith("Ran 2 runs (seed 2024).")
    assert "Profit per run:  $" in printed
    assert "Fill rate:" in printed and "Changeovers:" in printed
    assert printed.rstrip().endswith(f"Full result: {out / 'result.json'}")
    assert (out / "result.json").exists()


def test_summary_for_a_shop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "run"
    code, printed = _run([str(SHOP), "--reps", "2", "--out", str(out), "--summary"], capsys)

    assert code == 0
    assert "On time:" in printed and "% of orders" in printed


def test_summary_for_a_supply_network_lists_each_order() -> None:
    sample = {
        "order_forecasts": [
            {
                "order_id": "SO-1",
                "status": "feasible",
                "delivery_at": "2026-12-01T14:00:00+00:00",
                "on_time": True,
            },
            {"order_id": "SO-2", "status": "blocked", "delivery_at": None, "on_time": False},
        ],
        "shortages": [{"order_id": "SO-2", "item_id": "MTR-05", "missing_quantity": 4.0}],
    }
    result = {
        "domain": "supply_chain",
        "replications": 2,
        "seed": 7,
        "snapshot": {
            "as_of": "2026-11-18T07:00:00-06:00",
            "orders": [
                {"id": "SO-1", "item_id": "PMP-100", "quantity": 5, "due_at": "2026-12-04"},
                {"id": "SO-2", "item_id": "PMP-300", "quantity": 2, "due_at": "2026-12-08"},
            ],
        },
        "per_replication": [sample, sample],
    }

    lines = summary_lines(result, "runs/x/result.json")

    assert lines[0] == "Ran 2 runs (seed 7)."
    assert lines[1].split() == [
        "Order",
        "Item",
        "Qty",
        "Due",
        "Ready",
        "(typical)",
        "Ready",
        "(bad",
        "case)",
        "On",
        "time",
    ]
    assert lines[2].split()[:3] == ["SO-1", "PMP-100", "5"]
    assert "Tue Dec 01" in lines[2] and "100% (2 of 2)" in lines[2]
    assert lines[3].split()[:2] == ["SO-2", "PMP-300"] and "never" in lines[3]
    assert "  SO-2 needs 4 more MTR-05 (in 2 of 2 runs)" in lines
    assert lines[-1] == "Full result: runs/x/result.json"
