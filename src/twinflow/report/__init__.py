"""Layer 4 — self-contained HTML + versioned KPI JSON sidecar + Assumptions block."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report.assumptions import Assumption
from twinflow.report.html import HtmlReport
from twinflow.report.kpi_json import KpiJsonSidecar
from twinflow.run_stamp import RunStamp

if TYPE_CHECKING:
    from twinflow.model import CompiledModel


def render_html(
    kpis: KpiSet,
    assumptions: list[Assumption],
    run_stamp: RunStamp,
    out_path: str | Path,
    model: CompiledModel | None = None,
) -> Path:
    """Public API: one offline HTML file, Assumptions block first. When `model` is
    given, the report includes a value-stream diagram of the material flow."""
    return HtmlReport().render(kpis, assumptions, run_stamp, out_path, model)


def write_kpi_json(kpis: KpiSet, schema_version: int, out_path: str | Path) -> Path:
    """Public API: machine-readable versioned KPI sidecar."""
    return KpiJsonSidecar().write(kpis, schema_version, out_path)
