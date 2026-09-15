"""COMP-026 HtmlReport + COMP-027 KpiJsonSidecar — unit tests (T-044, red phase).

Contract under test (Layer 4, `report/`; tech.md D-026, D-042; structure.md
"report/ depends on instrumentation/, never the reverse"):

HtmlReport (`report/html.py`) renders ONE self-contained HTML file with Plotly
charts INLINED so the client can open it with the network disabled and nobody
in the room to explain it. It leads with the Assumptions block — every default
the engine substituted, named, before any KPI or chart content. Zero external
asset references anywhere in the document.

KpiJsonSidecar (`report/kpi_json.py`) writes a machine-readable, VERSIONED JSON
contract carrying every field of the objective-agnostic minimum set (D-042):
signed lateness per order, labor hours by pool and skill, machine hours by
individual machine, setup hours reported SEPARATELY from run hours, and WIP
over time.

Committed API (this test file fixes it; the T-045 implementer conforms):

    HtmlReport().render(
        kpis: KpiSet, assumptions: list[Assumption], run_stamp: RunStamp,
        out_path: str | Path,
    ) -> Path

    KpiJsonSidecar().write(
        kpis: KpiSet, schema_version: int, out_path: str | Path,
    ) -> Path

The `report/__init__.py` facades `render_html(kpis, assumptions, run_stamp,
out_path) -> Path` and `write_kpi_json(kpis, schema_version, out_path) -> Path`
may delegate directly to the two classes above; this file holds both call
surfaces to the same behavioral contract.

JSON sidecar shape this test file commits (boring, snake_case, mirrors
`KpiSet` field names per structure.md naming rules):

    schema_version              int                 exactly the value passed to .write()
    lateness_by_order           dict[str, float|null]  SIGNED days/seconds; null when
                                                        completion is unknown (never
                                                        dropped, never coerced to 0)
    labor_hours_by_pool_skill   dict[str, dict[str, float]]  nested {pool: {skill: hours}} —
                                                        JSON has no tuple keys, so the
                                                        KpiSet's (pool, skill) tuple key
                                                        becomes two nesting levels
    machine_hours_by_machine    dict[str, float]       by location_id
    run_hours                   float                  total run hours, all rows
    setup_hours                 float                  total setup hours — a DISTINCT
                                                        top-level key from run_hours,
                                                        never merged into it
    wip_over_time                list[dict]             one dict per `wip_by_location` row:
                                                        {"location_id": str, "t": float,
                                                        "wip": int}

RED-phase note: `HtmlReport.__init__` and `KpiJsonSidecar.__init__` both
currently `raise NotImplementedError("T-045")` unconditionally, and the
`report/__init__.py` facades also `raise NotImplementedError("T-045")`. Every
test below therefore fails at the first call into the missing behavior —
never at import time, never from a typo. The two `test_*_modules_import_cleanly`
guard tests below pass NOW and prove the RED failures are behavioral, not
structural.

No mocks: real `KpiSet`, real `Assumption`, real `RunStamp` instances, real
files written to and read back from `tmp_path`. `report/` has no external
boundary (network, DB, clock, subprocess) worth mocking — Plotly's own
renderer runs for real so the "no external asset" and "Plotly inlined"
assertions mean something.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import polars as pl
import pytest

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report import render_html, write_kpi_json
from twinflow.report.assumptions import Assumption
from twinflow.report.html import HtmlReport
from twinflow.report.kpi_json import KpiJsonSidecar
from twinflow.run_stamp import RunStamp

# ---------------------------------------------------------------------------
# Fixtures — real KpiSet / Assumption / RunStamp instances, no mocks
# ---------------------------------------------------------------------------


def _build_kpi_set() -> KpiSet:
    """A hand-built KpiSet covering every objective-agnostic field (D-042),
    with `run_hours` and `setup_hours` deliberately DIFFERENT values so a
    sidecar that accidentally merges or overwrites one with the other is
    caught by a strict inequality assertion, not just "both keys present".
    Lateness spans early (-5.0), late (+12.5) and unknown (None) so a
    sidecar that drops sign or coerces null to 0 is caught.
    """
    return KpiSet(
        completion_by_order={"WO-1": 95.0, "WO-2": 112.5, "WO-3": None},
        lateness_by_order={"WO-1": -5.0, "WO-2": 12.5, "WO-3": None},
        on_time_pct=50.0,
        utilization_by_cell={"cell_a": 0.82, "cell_b": 0.40},
        utilization_by_machine={"cell_a": 0.82, "cell_b": 0.40},
        wait_seconds_by_location={
            "cell_a": {"starved": 120.0, "blocked": 30.0, "material_starved": 0.0},
        },
        labor_pool_utilization={"default": 0.75},
        wip_by_location=pl.DataFrame(
            {
                "location_id": ["cell_a", "cell_a", "cell_b"],
                "t": [0.0, 50.0, 0.0],
                "wip": [1, 0, 2],
            }
        ),
        machine_hours_by_machine={"cell_a": 40.0, "cell_b": 25.5},
        labor_hours_by_pool_skill={("default", "default"): 65.5, ("default", "welder"): 10.0},
        run_hours=65.5,
        setup_hours=4.25,
        event_counts_by_location={"cell_a": 120, "cell_b": 80},
    )


def _build_assumptions() -> list[Assumption]:
    return [
        Assumption(
            field="setup",
            default_used="setup time zero",
            why_absent="no changeover matrix declared",
        ),
        Assumption(
            field="labor_pool",
            default_used="one undifferentiated labor pool",
            why_absent="only one labor pool declared",
        ),
        Assumption(
            field="pull_rule.cell_a",
            default_used="arrival order used as sequence",
            why_absent="no pull rule declared for location 'cell_a'",
        ),
    ]


def _build_run_stamp() -> RunStamp:
    return RunStamp(
        engine_version="0.1.0",
        commit_sha="a" * 40,
        model_hash="b" * 64,
        plan_hash="c" * 64,
        base_seed=42,
        python_version="3.13.13",
        dependency_hash="d" * 64,
    )


_EXTERNAL_URL_ATTR = re.compile(r'(?:src|href)\s*=\s*["\']https?://', re.IGNORECASE)

# The inlined, UNMODIFIED Plotly library block. The offline guarantee is about the
# report's OWN markup loading nothing external; the vendored library carries inert
# default URLs (a topojson CDN, map-tile attribution) reachable only by geo/mapbox
# traces this report never builds. Scrubbing them out of the bundle corrupts Plotly
# and blanks every chart, so we exclude the delimited library block from the scan
# instead.
_VENDOR_LIB_BLOCK = re.compile(
    r"<script id=['\"]twinflow-(?:plotly|mermaid)-lib['\"]>.*?</script>",
    re.IGNORECASE | re.DOTALL,
)


def _own_markup(html: str) -> str:
    """The report HTML with the vendored Plotly and Mermaid library blocks removed -
    those inline third-party bundles carry inert internal URLs that are not assets our
    own markup loads."""
    return _VENDOR_LIB_BLOCK.sub("", html)


# ---------------------------------------------------------------------------
# Structural guards — prove the RED failures below are behavioral, not
# import errors or typos (must PASS now)
# ---------------------------------------------------------------------------


def test_html_report_module_imports_cleanly() -> None:
    assert callable(HtmlReport)


def test_kpi_json_sidecar_module_imports_cleanly() -> None:
    assert callable(KpiJsonSidecar)


def test_report_facades_import_cleanly() -> None:
    assert callable(render_html)
    assert callable(write_kpi_json)


# ---------------------------------------------------------------------------
# HtmlReport (COMP-026) — offline, self-contained, assumptions-first
# ---------------------------------------------------------------------------


def test_html_report_render_writes_a_file_at_out_path(tmp_path: Path) -> None:
    out_path = tmp_path / "report.html"

    result = HtmlReport().render(
        _build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path
    )

    assert out_path.exists()
    assert out_path.stat().st_size > 0
    assert Path(result) == out_path


def test_html_report_has_zero_external_asset_references(tmp_path: Path) -> None:
    """Opens offline: no src=/href= pointing at http(s), and no protocol-relative
    CDN reference anywhere in the document."""
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    own = _own_markup(out_path.read_text(encoding="utf-8"))

    assert _EXTERNAL_URL_ATTR.search(own) is None
    assert "//cdn" not in own.lower()
    assert "cdn.plot.ly" not in own.lower()


def test_html_report_inlines_plotly_javascript(tmp_path: Path) -> None:
    """Plotly must be INLINED (include_plotlyjs=True/'inline'), not referenced —
    the bundled plotly.js source is well over 1MB of minified JS, so a
    self-contained file with real charts is unmistakably large and contains
    Plotly's own render call inline."""
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    assert "Plotly.newPlot(" in html
    assert out_path.stat().st_size > 500_000  # inlined plotly.js dwarfs a chartless page
    assert _EXTERNAL_URL_ATTR.search(_own_markup(html)) is None


def test_html_report_assumptions_block_is_first_content(tmp_path: Path) -> None:
    """Assumptions must appear BEFORE any KPI/chart content — this is the
    literal ordering requirement in REQ-033 and tech.md's Assumptions row."""
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    assumptions_index = html.lower().find("assumption")
    chart_index = html.find("Plotly.newPlot(")

    assert assumptions_index != -1, "no Assumptions marker found in the report at all"
    assert chart_index != -1, "no chart found in the report at all"
    assert assumptions_index < chart_index


def test_html_report_names_every_substituted_default(tmp_path: Path) -> None:
    """Every Assumption's field/default_used/why_absent text is visible in the
    report — not just a generic 'defaults were used' banner."""
    out_path = tmp_path / "report.html"
    assumptions = _build_assumptions()
    HtmlReport().render(_build_kpi_set(), assumptions, _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    for assumption in assumptions:
        assert assumption.default_used in html
        assert assumption.why_absent in html


def test_html_report_with_no_assumptions_still_renders_assumptions_section(
    tmp_path: Path,
) -> None:
    """Adversarial: an empty assumptions list must not silently drop the
    Assumptions section — the client still needs to see 'no defaults used'."""
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), [], _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    assert "assumption" in html.lower()


def test_html_report_facade_delegates_with_same_offline_and_ordering_contract(
    tmp_path: Path,
) -> None:
    out_path = tmp_path / "facade_report.html"
    result = render_html(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    assert Path(result) == out_path
    assert _EXTERNAL_URL_ATTR.search(_own_markup(html)) is None
    assert html.lower().find("assumption") < html.find("Plotly.newPlot(")


# ---------------------------------------------------------------------------
# KpiJsonSidecar (COMP-027) — versioned, objective-agnostic minimum set
# ---------------------------------------------------------------------------


def test_kpi_json_sidecar_write_creates_valid_json_file(tmp_path: Path) -> None:
    out_path = tmp_path / "kpis.json"

    result = KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)

    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert Path(result) == out_path


def test_kpi_json_sidecar_carries_schema_version(tmp_path: Path) -> None:
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=3, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 3


def test_kpi_json_sidecar_carries_signed_lateness_per_order(tmp_path: Path) -> None:
    """Signed lateness, never a boolean, and null (not 0, not dropped) for an
    order whose completion is unknown."""
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    lateness = payload["lateness_by_order"]
    assert lateness["WO-1"] == pytest.approx(-5.0)
    assert lateness["WO-2"] == pytest.approx(12.5)
    assert lateness["WO-3"] is None


def test_kpi_json_sidecar_carries_labor_hours_by_pool_and_skill(tmp_path: Path) -> None:
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    labor = payload["labor_hours_by_pool_skill"]
    assert labor["default"]["default"] == pytest.approx(65.5)
    assert labor["default"]["welder"] == pytest.approx(10.0)


def test_kpi_json_sidecar_carries_machine_hours_by_machine(tmp_path: Path) -> None:
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    machine_hours = payload["machine_hours_by_machine"]
    assert machine_hours["cell_a"] == pytest.approx(40.0)
    assert machine_hours["cell_b"] == pytest.approx(25.5)


def test_kpi_json_sidecar_reports_setup_hours_separately_from_run_hours(
    tmp_path: Path,
) -> None:
    """A cost objective prices changeover differently from production (D-042) —
    setup_hours and run_hours must be two distinct keys with distinct values,
    never merged or summed into one figure."""
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert payload["run_hours"] == pytest.approx(65.5)
    assert payload["setup_hours"] == pytest.approx(4.25)
    assert payload["run_hours"] != payload["setup_hours"]


def test_kpi_json_sidecar_carries_wip_over_time(tmp_path: Path) -> None:
    out_path = tmp_path / "kpis.json"
    KpiJsonSidecar().write(_build_kpi_set(), schema_version=1, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    wip_series = payload["wip_over_time"]
    assert isinstance(wip_series, list)
    assert len(wip_series) == 3
    cell_a_points = [row for row in wip_series if row["location_id"] == "cell_a"]
    assert {(row["t"], row["wip"]) for row in cell_a_points} == {(0.0, 1), (50.0, 0)}


def test_kpi_json_sidecar_facade_delegates_with_same_contract(tmp_path: Path) -> None:
    out_path = tmp_path / "facade_kpis.json"
    result = write_kpi_json(_build_kpi_set(), schema_version=2, out_path=out_path)
    payload = json.loads(out_path.read_text(encoding="utf-8"))

    assert Path(result) == out_path
    assert payload["schema_version"] == 2
    assert payload["lateness_by_order"]["WO-3"] is None
    assert payload["setup_hours"] != payload["run_hours"]


# ---------------------------------------------------------------------------
# Shared-resource utilization: one row per physical machine pool / labor pool,
# aggregated across the routing steps that share it (a single fridge used by
# three bread chains is ONE resource, not three). Regression guard for the
# per-step-utilization-only report that fragmented shared equipment.
# ---------------------------------------------------------------------------


def _fake_model_two_steps_one_machine():
    """A model-shaped stand-in: `bake_a` and `bake_b` share machine `oven_m`
    (capacity 2), staffed by one `bakers` pool of 1."""
    from types import SimpleNamespace

    loc = lambda lid: SimpleNamespace(  # noqa: E731 - terse test builder
        location_id=lid,
        machine=SimpleNamespace(machine_id="oven_m"),
        capacity=2,
        pull_rule=SimpleNamespace(setup_key_of={}),
        stock_destinations={},
    )
    return SimpleNamespace(
        locations=[loc("bake_a"), loc("bake_b")],
        labor_pools=[SimpleNamespace(name="bakers", headcount=1, skills={"bake"})],
        stocks=[],
        routing={},
    )


def test_shared_resource_rows_aggregate_by_physical_machine() -> None:
    kpis = _build_kpi_set()
    kpis = replace(  # busy hours attributed to the two steps that share oven_m
        kpis, busy_hours_by_location={"bake_a": 6.0, "bake_b": 4.0}
    )
    horizon_seconds = 20 * 3600.0  # 20 h run

    machine_rows, labor_rows = HtmlReport._shared_resource_rows(
        kpis, _fake_model_two_steps_one_machine(), horizon_seconds
    )

    # ONE oven_m row, not two: its two steps' busy hours (6+4=10) over
    # horizon x capacity (20 h x 2 slots = 40) -> 25%.
    assert len(machine_rows) == 1
    oven = machine_rows[0]
    assert oven["resource"] == "oven_m"
    assert oven["capacity"] == 2
    assert set(oven["steps"]) == {"bake_a", "bake_b"}
    # no calendar on this pool -> open 24/7, so operating and wall-clock agree.
    assert oven["util_wall"] == pytest.approx(10.0 / 40.0)
    assert oven["util_operating"] == pytest.approx(10.0 / 40.0)

    # the single baker pool's utilization comes from labor hours, not machine hours
    assert len(labor_rows) == 1
    assert labor_rows[0]["resource"] == "bakers"


def test_operating_hours_utilization_exceeds_wall_clock_when_shifts_are_limited() -> None:
    """A baker open only part of the day is busier against OPEN hours than against
    the 24/7 clock: true (operating) utilization must exceed wall-clock."""
    from datetime import datetime
    from types import SimpleNamespace

    from twinflow.primitives.calendar import CalendarConfig, WorkingInterval, parse_clock

    calendar = CalendarConfig(
        timezone="UTC",
        origin=datetime.fromisoformat("2026-09-14T00:00:00+00:00"),  # Monday
        weekly=(WorkingInterval(frozenset(range(7)), parse_clock("00:00"), parse_clock("06:00")),),
    )  # open 6 of every 24 hours
    model = SimpleNamespace(
        locations=[],
        labor_pools=[
            SimpleNamespace(name="bakers", headcount=1, skills={"bake"}, calendar=calendar)
        ],
    )
    # 3 busy labor-hours over a 24 h run: 3/24 = 12.5% wall, 3/6 = 50% of open hours.
    kpis = replace(_build_kpi_set(), labor_hours_by_pool_skill={("bakers", "bake"): 3.0})

    _machines, labor_rows = HtmlReport._shared_resource_rows(kpis, model, 24 * 3600.0)

    assert labor_rows[0]["util_wall"] == pytest.approx(3 / 24)
    assert labor_rows[0]["util_operating"] == pytest.approx(3 / 6)
    assert labor_rows[0]["operating_hours"] == pytest.approx(6.0)


def test_shared_resource_section_appears_only_with_horizon(tmp_path: Path) -> None:
    model = _fake_model_two_steps_one_machine()
    kpis = replace(_build_kpi_set(), busy_hours_by_location={"bake_a": 6.0, "bake_b": 4.0})

    with_h = tmp_path / "with.html"
    HtmlReport().render(kpis, [], _build_run_stamp(), with_h, model=model, horizon=20 * 3600.0)
    without_h = tmp_path / "without.html"
    HtmlReport().render(kpis, [], _build_run_stamp(), without_h, model=model)

    assert "Shared resources" in with_h.read_text(encoding="utf-8")
    assert "Shared resources" not in without_h.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-order completion timeline: a Gantt-lite of due -> finished, on a real
# date axis when the model carries a calendar origin.
# ---------------------------------------------------------------------------


def test_completion_timeline_uses_real_dates_when_origin_present() -> None:
    from datetime import datetime

    origin = datetime.fromisoformat("2026-09-14T04:00:00+00:00")
    fig = HtmlReport()._completion_timeline_figure(_build_kpi_set(), origin)

    # at least one trace carries python datetimes on x (real-date axis), not raw
    # second counts, and the on-time / late split is present.
    xs = [x for trace in fig.data for x in (trace.x or []) if x is not None]
    assert any(isinstance(x, datetime) for x in xs)
    names = {trace.name for trace in fig.data}
    assert {"on time", "late", "due", "finished"} <= names


def test_completion_timeline_falls_back_to_seconds_without_origin() -> None:
    fig = HtmlReport()._completion_timeline_figure(_build_kpi_set(), None)
    xs = [x for trace in fig.data for x in (trace.x or []) if x is not None]
    # WO-1 finished at 95.0 s with lateness -5.0 -> due at 100.0 s: raw seconds,
    # never coerced to a date.
    assert all(isinstance(x, (int, float)) for x in xs)
    assert 100.0 in xs


# ---------------------------------------------------------------------------
# Tabbed layout + grouped assumptions: the assumptions block (one entry per
# work center) must not monopolize the top of the report. It lives in its own
# tab, and repeated substitutions collapse into one counted row.
# ---------------------------------------------------------------------------


def test_report_has_tab_nav_and_assumptions_panel(tmp_path: Path) -> None:
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    assert "class='tabs'" in html
    assert "data-tab='assumptions'" in html
    assert "data-panel='assumptions'" in html
    # the overview panel is the one shown by default (not hidden)
    assert "data-panel='overview'>" in html


def test_repeated_assumptions_collapse_to_a_counted_row(tmp_path: Path) -> None:
    """23 'no pull rule for location X' entries become ONE row with count 23,
    not 23 near-identical cards."""
    assumptions = [
        Assumption(
            field=f"pull_rule.stn_{i}",
            default_used="arrival order used as sequence",
            why_absent=f"no pull rule declared for location 'stn_{i}'",
        )
        for i in range(23)
    ]
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), assumptions, _build_run_stamp(), out_path)
    html = out_path.read_text(encoding="utf-8")

    # one summarized group, and the count 23 is shown
    assert "23 default(s) substituted, grouped into 1 kind(s)" in html
    assert "for a location" in html  # the collapsed pattern, not 23 verbatim sentences


def test_flow_svg_is_sized_to_its_viewbox_not_collapsed(tmp_path: Path) -> None:
    """Regression: the material-flow mermaid SVG carries width='100%' and only a
    viewBox, so inside the inline-block flow stage it renders 0x0 and the diagram
    looks empty. The report must (a) NOT force `width:auto` on the flow SVG, which
    collapses it, and (b) pin the SVG's natural viewBox size in px after mermaid
    renders it."""
    out_path = tmp_path / "report.html"
    HtmlReport().render(_build_kpi_set(), _build_assumptions(), _build_run_stamp(), out_path)
    doc = out_path.read_text(encoding="utf-8")

    assert "width:auto!important" not in doc
    assert "sizeFlowSvg" in doc
    # sets the svg's width/height from viewBox coordinates 2 and 3
    assert "s.style.width=vb[2]" in doc
    assert "s.style.height=vb[3]" in doc
