"""Layer 4 — self-contained HTML + versioned KPI JSON sidecar + Assumptions block."""

from __future__ import annotations

from pathlib import Path

from factory_twin.instrumentation.kpis import KpiSet
from factory_twin.report.assumptions import Assumption
from factory_twin.report.html import HtmlReport
from factory_twin.report.kpi_json import KpiJsonSidecar
from factory_twin.run_stamp import RunStamp


def render_html(
    kpis: KpiSet,
    assumptions: list[Assumption],
    run_stamp: RunStamp,
    out_path: str | Path,
) -> Path:
    """Public API: one offline HTML file, Assumptions block first."""
    return HtmlReport().render(kpis, assumptions, run_stamp, out_path)


def write_kpi_json(kpis: KpiSet, schema_version: int, out_path: str | Path) -> Path:
    """Public API: machine-readable versioned KPI sidecar."""
    return KpiJsonSidecar().write(kpis, schema_version, out_path)
