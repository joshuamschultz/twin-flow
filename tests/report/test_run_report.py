"""B-34: the digital-twin run report is the run viewer (DESIGN.md section 4).

`twinflow run` and `twinflow report` write `report.html` through `run_view_model` + the
offline decision template. This file replaces the HtmlReport tests in
`tests/unit/test_report.py` one for one. Each old test is named in a comment with its new
assertion, or with the reason it no longer applies.

Feature inventory (BUILD-PLAN.md B-34), each item checked by a test below:
- Assumptions -> Details (`about.assumptions`)
- Headline chips -> hero figures (`answer`, `centres`)
- Confidence ranges -> ranges on hero figures, order whiskers, Details table (`Stat` low/high)
- Shared resources -> Choke point + Details (`details.machine_pools`, `details.labor_pools`)
- Material flow (Mermaid) -> ChokeFlow (`routes`, `centres`)
- Charts -> Choke cards, OrderRiskList, WIP line in Details (`wip_by_time`)
- Detail tables -> Details, same numbers (`details.*`, `labor`)
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from twinflow.cli.main import main
from twinflow.instrumentation.kpis import KpiSet
from twinflow.report import write_kpi_json, write_run_report
from twinflow.report.decision import render
from twinflow.report.run_detail import shared_resource_rows, start_instant
from twinflow.report.run_view import run_view_model

ROOT = Path(__file__).resolve().parents[2]
SHOP = ROOT / "examples" / "cnc-shop"
_EXTERNAL_URL_ATTR = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)


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
        code = main(
            ["run", str(SHOP / "model.yaml"), "--plan", str(SHOP / "plan.csv"), "--reps", str(reps)]
        )
        assert code == 0
        (run_dir,) = (cwd / "runs").iterdir()
        return run_dir


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _run(tmp_path_factory.mktemp("report"), 5)


class _Data(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.inside = tag == "script" and ("id", "twinflow-data") in attrs

    def handle_endtag(self, tag: str) -> None:
        self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.text += data


def _payload(html: str) -> dict[str, Any]:
    parser = _Data()
    parser.feed(html)
    return dict(json.loads(parser.text))


def _with_assumptions(run_dir: Path, assumptions: list[dict[str, str]]) -> Path:
    detail_path = run_dir / "run_detail.json"
    detail = json.loads(detail_path.read_text())
    detail["assumptions"] = assumptions
    detail_path.write_text(json.dumps(detail))
    return write_run_report(run_dir)


# old: test_html_report_module_imports_cleanly, test_report_facades_import_cleanly
def test_report_facades_import_cleanly() -> None:
    assert callable(write_run_report)
    assert callable(write_kpi_json)


# old: test_html_report_render_writes_a_file_at_out_path
def test_run_writes_report_html_and_rebuild_returns_its_path(shop: Path) -> None:
    out = shop / "report.html"
    assert out.is_file() and out.stat().st_size > 0
    assert write_run_report(shop) == out


# old: test_html_report_has_zero_external_asset_references
def test_report_has_zero_external_asset_references(shop: Path) -> None:
    html = (shop / "report.html").read_text(encoding="utf-8")
    assert _EXTERNAL_URL_ATTR.search(html) is None
    assert "//cdn" not in html.lower()


# old: test_html_report_inlines_plotly_javascript. Plotly is gone on purpose (DESIGN.md 0.1,
# B-34). New: the view model is inlined, the file is small, and no Plotly or Mermaid is left.
def test_report_inlines_the_view_model_not_plotly(shop: Path) -> None:
    html = (shop / "report.html").read_text(encoding="utf-8")
    assert _payload(html)["view"] == "run"
    assert "Plotly.newPlot(" not in html
    assert "mermaid" not in html.lower()
    assert (shop / "report.html").stat().st_size < 900 * 1024


# old: test_html_report_assumptions_block_is_first_content. The design moves assumptions to
# Details (DESIGN.md 4.5, the B-34 inventory). New: they are in the data the page reads.
def test_assumptions_travel_in_the_view_model(shop: Path) -> None:
    about = _payload((shop / "report.html").read_text(encoding="utf-8"))["about"]
    assert any("Changeover time zero" in line for line in about["assumptions"])


# old: test_html_report_names_every_substituted_default
def test_report_names_every_substituted_default(shop: Path) -> None:
    assumptions = [
        {
            "field": "setup",
            "default_used": "setup time zero",
            "why_absent": "no changeover matrix declared",
        },
        {
            "field": "labor_pool",
            "default_used": "one undifferentiated labor pool",
            "why_absent": "only one labor pool declared",
        },
    ]
    html = _with_assumptions(shop, assumptions).read_text(encoding="utf-8")
    for a in assumptions:
        plain_used = a["default_used"].replace("setup", "changeover")  # the page's words
        assert plain_used[1:] in html
        assert a["why_absent"] in html


# old: test_html_report_with_no_assumptions_still_renders_assumptions_section
def test_no_assumptions_still_says_no_defaults_were_used(shop: Path) -> None:
    html = _with_assumptions(shop, []).read_text(encoding="utf-8")
    assert _payload(html)["about"]["assumptions"] == []
    assert "No defaults were used. Every value came from the model." in html


# old: test_html_report_facade_delegates_with_same_offline_and_ordering_contract
def test_facade_writes_the_same_bytes_as_render(shop: Path) -> None:
    out = write_run_report(shop)
    assert out.read_text(encoding="utf-8") == render(run_view_model(shop))


# old: test_repeated_assumptions_collapse_to_a_counted_row
def test_repeated_assumptions_collapse_to_a_counted_line(shop: Path) -> None:
    many = [
        {
            "field": f"pull_rule.stn_{i}",
            "default_used": "arrival order used as sequence",
            "why_absent": f"no pull rule declared for location 'stn_{i}'",
        }
        for i in range(23)
    ]
    html = _with_assumptions(shop, many).read_text(encoding="utf-8")
    (line,) = _payload(html)["about"]["assumptions"]
    assert "for a location (23 locations: " in line


# -- shared resources: rows moved from HtmlReport._shared_resource_rows ------------------------


def _kpis() -> KpiSet:
    return KpiSet(
        completion_by_order={"WO-1": 95.0},
        lateness_by_order={"WO-1": -5.0},
        on_time_pct=100.0,
        utilization_by_cell={},
        utilization_by_machine={},
        wait_seconds_by_location={},
        labor_pool_utilization={},
        wip_by_location=pl.DataFrame({"location_id": [], "t": [], "wip": []}),
        machine_hours_by_machine={},
        labor_hours_by_pool_skill={},
        run_hours=0.0,
        setup_hours=0.0,
        event_counts_by_location={},
    )


def _fake_model_two_steps_one_machine() -> Any:
    def loc(lid: str) -> SimpleNamespace:
        return SimpleNamespace(
            location_id=lid, machine=SimpleNamespace(machine_id="oven_m"), capacity=2
        )

    return SimpleNamespace(
        locations=[loc("bake_a"), loc("bake_b")],
        labor_pools=[SimpleNamespace(name="bakers", headcount=1, skills={"bake"})],
    )


# old: test_shared_resource_rows_aggregate_by_physical_machine
def test_shared_resource_rows_aggregate_by_physical_machine() -> None:
    kpis = replace(_kpis(), busy_hours_by_location={"bake_a": 6.0, "bake_b": 4.0})
    machine_rows, labor_rows = shared_resource_rows(
        kpis, _fake_model_two_steps_one_machine(), 20 * 3600.0
    )
    assert len(machine_rows) == 1
    oven = machine_rows[0]
    assert oven["resource"] == "oven_m"
    assert oven["capacity"] == 2
    assert set(oven["steps"]) == {"bake_a", "bake_b"}
    assert oven["util_wall"] == pytest.approx(10.0 / 40.0)
    assert oven["util_operating"] == pytest.approx(10.0 / 40.0)
    assert len(labor_rows) == 1
    assert labor_rows[0]["resource"] == "bakers"


def _calendar() -> Any:
    from twinflow.primitives.calendar import CalendarConfig, WorkingInterval, parse_clock

    return CalendarConfig(
        timezone="UTC",
        origin=datetime.fromisoformat("2026-09-14T00:00:00+00:00"),
        weekly=(WorkingInterval(frozenset(range(7)), parse_clock("00:00"), parse_clock("06:00")),),
    )


# old: test_operating_hours_utilization_exceeds_wall_clock_when_shifts_are_limited
def test_operating_hours_utilization_exceeds_wall_clock_when_shifts_are_limited() -> None:
    model = SimpleNamespace(
        locations=[],
        labor_pools=[
            SimpleNamespace(name="bakers", headcount=1, skills={"bake"}, calendar=_calendar())
        ],
    )
    kpis = replace(_kpis(), labor_hours_by_pool_skill={("bakers", "bake"): 3.0})
    _machines, labor_rows = shared_resource_rows(kpis, model, 24 * 3600.0)  # type: ignore[arg-type]
    assert labor_rows[0]["util_wall"] == pytest.approx(3 / 24)
    assert labor_rows[0]["util_operating"] == pytest.approx(3 / 6)
    assert labor_rows[0]["operating_hours"] == pytest.approx(6.0)


# old: test_shared_resource_section_appears_only_with_horizon. A run always has a horizon now,
# so the rows are always there; they sit in Details.
def test_shared_resources_are_in_details(shop: Path) -> None:
    details = run_view_model(shop)["details"]
    assert {r["resource"] for r in details["labor_pools"]} == {"floor_pool"}
    assert "mill_shaft_m" in {r["resource"] for r in details["machine_pools"]}


# -- times: old completion timeline tests --------------------------------------------------------


# old: test_completion_timeline_uses_real_dates_when_origin_present. The timeline is now
# OrderRiskList in the run's time unit; the real start instant is carried in `about.start`.
def test_start_instant_comes_from_the_shift_calendar() -> None:
    model = SimpleNamespace(labor_pools=[SimpleNamespace(calendar=_calendar())])
    assert start_instant(model) == datetime.fromisoformat("2026-09-14T00:00:00+00:00")  # type: ignore[arg-type]


# old: test_completion_timeline_falls_back_to_seconds_without_origin
def test_without_a_calendar_times_are_plain_run_time(shop: Path) -> None:
    model = run_view_model(shop)
    assert model["about"]["start"] is None
    wo1 = next(o for o in model["orders"] if o["id"] == "wo-1")
    assert wo1["release"] == 0.0
    assert wo1["due"] == 25.0  # 1500 s in minutes


# -- layout: old tab and flow tests ----------------------------------------------------------


# old: test_report_has_tab_nav_and_assumptions_panel. Tabs became the section bar (DESIGN.md
# 0.1); its keyboard and scroll behaviour is checked in web/tests/browser/report/run.spec.ts.
def test_report_page_has_the_section_bar_labels(shop: Path) -> None:
    html = (shop / "report.html").read_text(encoding="utf-8")
    for label in ("Orders at risk", "Choke point", "Best fix", "Details"):
        assert label in html


# old: test_flow_svg_is_sized_to_its_viewbox_not_collapsed. The Mermaid SVG is gone; the flow
# is ChokeFlow, plain CSS grid built from `routes` and `centres`.
def test_flow_is_built_from_routes_and_centres(shop: Path) -> None:
    model = run_view_model(shop)
    centres = {c["id"] for c in model["centres"]}
    for route in model["routes"]:
        assert set(route["steps"]) <= centres


# -- the rest of the inventory -------------------------------------------------------------


def test_headline_chips_and_ranges_move_to_the_hero(shop: Path) -> None:
    answer = run_view_model(shop)["answer"]
    on = answer["on_time"]
    assert on["low"] <= on["mean"] <= on["high"]
    assert answer["choke"]["centre"] == "mill_shaft"


def test_detail_tables_keep_the_same_numbers(shop: Path) -> None:
    kpis = json.loads((shop / "kpis.json").read_text())
    model = run_view_model(shop)
    details = model["details"]
    machine = {m["machine"]: m["hours"] for m in details["machine_hours_by_machine"]}
    for name, hours in kpis["machine_hours_by_machine"].items():
        assert machine[name] == pytest.approx(hours, abs=1e-3)
    labor = {(r["pool"], r["skill"]): r["hours"] for r in model["labor"]}
    for pool, skills in kpis["labor_hours_by_pool_skill"].items():
        for skill, hours in skills.items():
            assert labor[(pool, skill)] == pytest.approx(hours, abs=1e-3)
    late = {o["id"]: o["late_by"] for o in details["orders"]}
    for order, seconds in kpis["lateness_by_order"].items():
        assert late[order] == pytest.approx(seconds / 60.0, abs=1e-3)
    assert {c["id"] for c in details["centres"]} == set(
        json.loads((shop / "intervals.json").read_text())["utilization_by_cell"]
    )
    assert model["wip_by_time"]["t"]


def test_report_command_rebuilds_the_report(shop: Path) -> None:
    (shop / "report.html").unlink()
    with _cwd(shop.parent.parent):
        assert main(["report", shop.name, "--out", "html"]) == 0
    assert (shop / "report.html").is_file()


def test_old_report_module_is_gone() -> None:
    # B-33 and B-34 moved every report onto the decision views; the Plotly and Mermaid
    # page helpers and the vendored Mermaid file went with them.
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("twinflow.report.html")
    assert not (ROOT / "src" / "twinflow" / "report" / "_assets").exists()
