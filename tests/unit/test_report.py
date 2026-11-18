"""COMP-027 KpiJsonSidecar — unit tests (T-044).

The COMP-026 HtmlReport tests that lived here moved to tests/report/test_run_report.py
when the run report became the run viewer (TF-006 B-34), one for one.

KpiJsonSidecar (`report/kpi_json.py`) writes a machine-readable, VERSIONED JSON
contract carrying every field of the objective-agnostic minimum set (D-042):
signed lateness per order, labor hours by pool and skill, machine hours by
individual machine, setup hours reported SEPARATELY from run hours, and WIP
over time. `kpis.json` is unchanged by B-34.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import polars as pl
import pytest

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report import write_kpi_json
from twinflow.report.assumptions import Assumption
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


def test_kpi_json_sidecar_module_imports_cleanly() -> None:
    assert callable(KpiJsonSidecar)


def test_report_facades_import_cleanly() -> None:
    assert callable(write_kpi_json)


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
