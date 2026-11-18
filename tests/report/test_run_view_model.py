"""B-26: the run view model and gaps G1 to G9, on the real cnc-shop run (30 test runs, seed 0).

Numbers come from `twinflow run examples/cnc-shop/model.yaml --plan .../plan.csv --reps 30`
and `twinflow balance ... --sweep examples/cnc-shop/sweep.json --reps 30 --run <id>`.
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
from twinflow.report.decision import run_view_model as exported
from twinflow.report.levers import lever_label, lever_rows
from twinflow.report.run_view import display_id, run_view_model, time_unit

ROOT = Path(__file__).resolve().parents[2]
SHOP = ROOT / "examples" / "cnc-shop"
SCHEMA = ROOT / "src" / "twinflow" / "report" / "schemas" / "run.schema.json"


@contextmanager
def _cwd(path: Path) -> Iterator[None]:
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _run(cwd: Path, reps: int) -> Path:
    with _cwd(cwd):
        before = set((cwd / "runs").glob("*")) if (cwd / "runs").exists() else set()
        code = main(
            [
                "run",
                str(SHOP / "model.yaml"),
                "--plan",
                str(SHOP / "plan.csv"),
                "--reps",
                str(reps),
            ]
        )
        assert code == 0
        (new,) = set((cwd / "runs").glob("*")) - before
        return new


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run(tmp_path_factory.mktemp("shop"), 30)


@pytest.fixture(scope="module")
def shop_with_sweep(tmp_path_factory: pytest.TempPathFactory) -> Path:
    cwd = tmp_path_factory.mktemp("sweep")
    run_dir = _run(cwd, 30)
    with _cwd(cwd):
        code = main(
            [
                "balance",
                str(SHOP / "model.yaml"),
                "--plan",
                str(SHOP / "plan.csv"),
                "--sweep",
                str(SHOP / "sweep.json"),
                "--reps",
                "30",
                "--run",
                run_dir.name,
            ]
        )
    assert code == 0
    return run_dir


@pytest.fixture(scope="module")
def model(shop: Path) -> dict[str, Any]:
    return run_view_model(shop)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


# -- G1, G2, G3, G5, G9: what `twinflow run` now keeps in the run folder --------------------


def test_the_run_folder_keeps_the_plan(shop: Path) -> None:
    plan = json.loads((shop / "plan.json").read_text())
    assert [o["id"] for o in plan["orders"]][:3] == ["wo-1", "wo-2", "wo-3"]
    assert plan["orders"][8] == {
        "id": "wo-9",
        "part": "shaft",
        "qty": 20,
        "release": 1200.0,
        "due": 3500.0,
        "priority": 0,
    }


def test_the_run_folder_keeps_per_run_lateness_steps_routes_and_labels(shop: Path) -> None:
    detail = json.loads((shop / "run_detail.json").read_text())
    assert len(detail["per_rep"]["late_orders"]) == 30
    assert len(detail["per_rep"]["due_state_by_order"]["wo-9"]) == 30
    assert [s["centre"] for s in detail["steps"]["wo-9"]] == [
        "saw_shaft",
        "mill_shaft",
        "deburr_shaft",
        "inspect_shaft",
    ]
    assert {
        "part": "shaft",
        "steps": ["saw_shaft", "mill_shaft", "deburr_shaft", "inspect_shaft"],
    } in detail["routes"]
    assert detail["labels"]["centres"]["mill_shaft"] == "Shaft mill"
    assert detail["queue_wait_s"]["mill_shaft"] > detail["queue_wait_s"]["saw_shaft"]


def test_existing_sidecars_keep_their_shape(shop: Path) -> None:
    kpis = json.loads((shop / "kpis.json").read_text())
    assert set(kpis) == {
        "schema_version",
        "lateness_by_order",
        "labor_hours_by_pool_skill",
        "machine_hours_by_machine",
        "run_hours",
        "setup_hours",
        "wip_over_time",
    }
    intervals = json.loads((shop / "intervals.json").read_text())
    assert set(intervals) == {
        "schema_version",
        "reps",
        "level",
        "on_time_pct",
        "lateness_by_order",
        "completion_by_order",
        "utilization_by_cell",
    }
    assert "base_seed" in json.loads((shop / "run_meta.json").read_text())


# -- the plan's accept list ---------------------------------------------------------------


def test_on_time_share_matches_the_intervals(model: dict[str, Any]) -> None:
    assert model["view"] == "run"
    assert model["answer"]["on_time"]["mean"] == 0.873
    assert model["answer"]["on_time"]["low"] == pytest.approx(0.833, abs=0.001)
    assert model["answer"]["on_time"]["high"] == pytest.approx(0.913, abs=0.001)


def test_watch_starts_with_wo9_and_the_choke_is_the_shaft_mill(model: dict[str, Any]) -> None:
    answer = model["answer"]
    assert answer["watch"][0] == "wo-9"
    assert answer["choke"]["centre"] == "mill_shaft"
    assert answer["choke"]["label"] == "Shaft mill"
    assert answer["choke"]["busy"] == pytest.approx(0.987, abs=0.001)
    assert answer["level"] == "most_on_time"
    assert answer["headline"] == "Most will. Watch WO-9."


def test_late_chance_is_a_counted_share(shop: Path, model: dict[str, Any]) -> None:
    detail = json.loads((shop / "run_detail.json").read_text())
    states = detail["per_rep"]["due_state_by_order"]["wo-9"]
    wo9 = next(o for o in model["orders"] if o["id"] == "wo-9")
    assert wo9["late_chance"] == round(states.count("late") / 30, 3)
    assert wo9["late_chance"] * 30 == pytest.approx(round(wo9["late_chance"] * 30), abs=0.02)
    assert wo9["status"] == "late"


def test_orders_are_sorted_by_late_chance(model: dict[str, Any]) -> None:
    chances = [o["late_chance"] for o in model["orders"]]
    assert chances == sorted(chances, reverse=True)


def test_times_are_in_minutes_for_a_one_hour_run(model: dict[str, Any]) -> None:
    about = model["about"]
    assert about["time_unit"] == "minutes"
    assert 55 < about["horizon"] < 70
    wo9 = next(o for o in model["orders"] if o["id"] == "wo-9")
    assert wo9["release"] == 20.0
    assert wo9["due"] == pytest.approx(58.333, abs=0.001)
    assert wo9["finish"]["mean"] == pytest.approx(59.43, abs=0.01)
    assert wo9["room"] == pytest.approx(wo9["due"] - wo9["finish"]["mean"], abs=0.001)


def test_reasons_name_the_place_an_order_waits(model: dict[str, Any]) -> None:
    by_id = {o["id"]: o for o in model["orders"]}
    assert by_id["wo-9"]["reason"].endswith("It waits in line at the shaft mill.")
    assert by_id["wo-9"]["reason"].startswith("Likely 1.1 min late.")
    assert by_id["wo-2"]["reason"].startswith("Done ")
    steps = by_id["wo-9"]["steps"]
    assert max(steps, key=lambda s: s["wait"])["centre"] == "mill_shaft"


def test_centres_carry_status_by_the_busy_rule(model: dict[str, Any]) -> None:
    by_id = {c["id"]: c for c in model["centres"]}
    assert by_id["mill_shaft"]["status"] == "choke"
    assert by_id["mill_bracket"]["status"] == "next"
    assert by_id["saw_shaft"]["status"] == "ok"
    assert by_id["mill_shaft"]["queue_wait"] > 0


def test_lede_and_takeaways_read_like_the_mockup(model: dict[str, Any]) -> None:
    lede = model["answer"]["lede"]
    assert lede.startswith("In 30 test runs, about 9 of 10 orders finished on time.")
    assert "WO-9 was late in more than half of the runs." in lede
    assert lede.endswith(
        "Every order at risk is a shaft, and every shaft waits for the shaft mill."
    )
    assert model["takeaways"]["orders"].startswith(
        "The risk is all in one place. Shaft orders released at minute 10 or later queue at the"
        " shaft mill. Bracket orders finish "
    )
    choke = model["takeaways"]["choke"]
    assert choke.startswith("The shaft mill is the choke point. It is busy 99% of the time.")
    assert (
        "The bracket mill is next at 83% and will choke if bracket orders grow by a fifth." in choke
    )


def test_without_a_sweep_the_levers_are_untested_ideas_with_a_command(
    model: dict[str, Any],
) -> None:
    levers = model["levers"]
    assert levers and not any(lv["tested"] for lv in levers)
    assert levers[0]["label"] == "Add a second shaft mill"
    assert levers[0]["command"].startswith("twinflow balance ")
    assert model["sweep"] is None


def test_a_linked_sweep_puts_the_best_lever_first(shop_with_sweep: Path) -> None:
    levers = run_view_model(shop_with_sweep)["levers"]
    best = levers[0]
    assert best["tested"] is True
    assert best["label"] == "Add a second shaft mill"
    assert best["on_time"] == 0.987
    assert best["note"].startswith("Late orders drop from 1.3 to ")
    third = next(lv for lv in levers if lv["label"] == "Add a third shaft mill")
    assert third["note"].startswith("Only ")
    assert "Not worth the extra." in third["note"]
    assert all(
        not lv["id"].startswith("locations[mill_shaft].capacity")
        for lv in levers
        if not lv["tested"]
    )


def test_one_test_run_has_no_late_chance_and_says_so(tmp_path: Path) -> None:
    one = run_view_model(_run(tmp_path, 1))
    assert all(o["late_chance"] is None for o in one["orders"])
    assert "This is one test run. Run 30 to see how sure this is." in one["answer"]["lede"]
    assert one["answer"]["command"].endswith("--reps 30")


def test_the_model_validates_against_its_schema(model: dict[str, Any]) -> None:
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(model)]
    assert errors == []


def test_no_em_dash_in_any_string(model: dict[str, Any]) -> None:
    assert not [s for s in _strings(model) if "—" in s or "–" in s]


def test_decision_module_exports_the_builder() -> None:
    assert exported is run_view_model


# -- pure helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "unit"),
    [(3720, "minutes"), (3 * 3600, "hours"), (2 * 86400, "hours"), (4 * 86400, "days")],
)
def test_time_unit_follows_the_horizon(seconds: float, unit: str) -> None:
    assert time_unit(seconds) == unit


def test_time_unit_override_wins() -> None:
    assert time_unit(60, "days") == "days"


def test_display_id() -> None:
    assert display_id("wo-9") == "WO-9"
    assert display_id("order_12") == "order_12"


@pytest.mark.parametrize(
    ("path", "value", "base", "label"),
    [
        ("locations[mill_shaft].capacity", 2, 1, "Add a second shaft mill"),
        ("locations[mill_shaft].capacity", 3, 1, "Add a third shaft mill"),
        ("locations[mill_shaft].capacity", 1, 1, "Keep one shaft mill"),
        ("locations[mill_shaft].dispatch", "edd", "fifo", "Shaft mill: run jobs by due date"),
        ("labor.pools[floor_pool].headcount", 7, 6, "7 people in floor pool"),
        ("locations[oven].time_model.rate", 1.2, 1.0, "Make the oven 20% faster"),
        ("routing[x].y", 3, None, "Set routing[x].y to 3"),
    ],
)
def test_lever_labels(path: str, value: Any, base: Any, label: str) -> None:
    assert lever_label(path, value, base, {"mill_shaft": "Shaft mill"}) == label


def test_lever_rows_use_the_sweep_base_point_and_rank_best_first() -> None:
    summary = [
        {"levers": {"locations[m].capacity": 1}, "on_time_pct": 80.0, "late_orders_per_run": 2.0},
        {"levers": {"locations[m].capacity": 2}, "on_time_pct": 95.0, "late_orders_per_run": 0.5},
        {"levers": {"locations[m].capacity": 3}, "on_time_pct": 96.0, "late_orders_per_run": 0.4},
    ]
    rows, today = lever_rows(summary, {"locations[m].capacity": 1})
    assert today == {"on_time": 0.8, "late_orders_per_run": 2.0}
    assert [r["label"] for r in rows] == ["Add a second m", "Add a third m"]
    assert rows[0]["gain_points"] == 15.0
    assert rows[0]["note"] == "Late orders drop from 2.0 to 0.5 per run."
    assert rows[1]["note"] == "Only 1 point better than: add a second m. Not worth the extra."


def test_repeated_assumptions_collapse_to_a_counted_line(model: dict[str, Any]) -> None:
    lines = model["about"]["assumptions"]
    pull = [line for line in lines if "no pull rule declared for a location" in line]
    assert len(pull) == 1
    assert "(8 locations: deburr_bracket, deburr_shaft," in pull[0]
    assert len(lines) == len(set(lines))
