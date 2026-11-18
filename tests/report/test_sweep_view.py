"""B-35: `twinflow balance` and `twinflow optimize` write sweep.html (DESIGN.md 4.4).

Real cnc-shop numbers: 30 test runs, seed 0, sweep over the shaft mill count (1, 2, 3).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from twinflow.cli.main import main
from twinflow.report.sweep_view import sweep_view_model

ROOT = Path(__file__).resolve().parents[2]
SHOP = ROOT / "examples" / "cnc-shop"
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "sweep.schema.json"


@contextmanager
def _cwd(path: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _new(cwd: Path, argv: list[str]) -> Path:
    runs = cwd / "runs"
    before = set(runs.iterdir()) if runs.exists() else set()
    with _cwd(cwd):
        assert main(argv) == 0
    (new,) = set(runs.iterdir()) - before
    return new


MODEL = ["--plan", str(SHOP / "plan.csv")]


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    cwd = tmp_path_factory.mktemp("sweep")
    base = _new(cwd, ["run", str(SHOP / "model.yaml"), *MODEL, "--reps", "30"])
    sweep = _new(
        cwd,
        [
            "balance",
            str(SHOP / "model.yaml"),
            *MODEL,
            "--sweep",
            str(SHOP / "sweep.json"),
            "--reps",
            "30",
            "--run",
            base.name,
        ],
    )
    return base, sweep


@pytest.fixture(scope="module")
def model(linked: tuple[Path, Path]) -> dict[str, Any]:
    return sweep_view_model(linked[1])


def test_balance_writes_sweep_html(linked: tuple[Path, Path]) -> None:
    html = (linked[1] / "sweep.html").read_text(encoding="utf-8")
    assert '"view":"sweep"' in html
    assert "Plotly" not in html


def test_the_best_change_comes_first(model: dict[str, Any]) -> None:
    assert model["view"] == "sweep"
    assert model["answer"]["headline"] == "Add a second shaft mill. 87% to 99% on time."
    best = model["levers"][0]
    assert best["label"] == "Add a second shaft mill"
    assert best["on_time"] == 0.987
    assert model["answer"]["today"]["on_time"] == 0.873


def test_points_table_has_every_point_with_today_marked(model: dict[str, Any]) -> None:
    points = model["points"]
    assert len(points) == 3
    today = [p for p in points if p["today"]]
    assert len(today) == 1 and today[0]["label"] == "What you do today"
    assert all(p["reps"] == 30 for p in points)


def test_the_base_run_viewer_shows_the_levers(linked: tuple[Path, Path]) -> None:
    html = (linked[0] / "report.html").read_text(encoding="utf-8")
    assert "Late orders drop from 1.3 to" in html
    assert model_link(linked) == linked[0].name


def model_link(linked: tuple[Path, Path]) -> str:
    return str(sweep_view_model(linked[1])["about"]["base_run_id"])


def test_the_model_validates_against_its_schema(model: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(model)]
    assert errors == []


def test_optimize_writes_a_sweep_folder_and_page(tmp_path: Path) -> None:
    folder = _new(
        tmp_path,
        [
            "optimize",
            str(SHOP / "model.yaml"),
            *MODEL,
            "--lever",
            "locations[mill_shaft].capacity:1:2",
            "--budget",
            "2",
            "--reps",
            "3",
        ],
    )
    assert (folder / "sweep.html").is_file()
    view = sweep_view_model(folder)
    assert view["about"]["kind"] == "optimize"
    assert {p["label"] for p in view["points"]} >= {"Add a second shaft mill"}


def test_a_sweep_with_no_gain_says_so(tmp_path: Path) -> None:
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "summary.json").write_text(
        json.dumps(
            [
                {
                    "sweep_point": "point-0000",
                    "levers": {"locations[m].capacity": 1},
                    "reps": 5,
                    "on_time_pct": 90.0,
                    "busy_hours": 1.0,
                    "late_orders_per_run": 1.0,
                },
                {
                    "sweep_point": "point-0001",
                    "levers": {"locations[m].capacity": 2},
                    "reps": 5,
                    "on_time_pct": 90.0,
                    "busy_hours": 1.0,
                    "late_orders_per_run": 1.0,
                },
            ]
        )
    )
    (folder / "sweep_meta.json").write_text(
        json.dumps({"base_values": {"locations[m].capacity": 1}, "reps": 5})
    )
    view = sweep_view_model(folder)
    assert view["answer"]["headline"] == "No tested change helps."
