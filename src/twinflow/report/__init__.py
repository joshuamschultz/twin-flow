"""Layer 4: the offline run viewer, the versioned KPI JSON sidecar and the Assumptions block."""

from __future__ import annotations

from pathlib import Path

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report.decision import write
from twinflow.report.kpi_json import KpiJsonSidecar
from twinflow.report.run_view import run_view_model

REPORT_FILE = "report.html"


def write_run_report(run_dir: str | Path) -> Path:
    """Public API: write `report.html` (the run viewer, one offline file) for a run folder,
    from the files in that folder alone (DESIGN.md section 4, B-34)."""
    folder = Path(run_dir)
    return write(folder / REPORT_FILE, run_view_model(folder))


def write_kpi_json(kpis: KpiSet, schema_version: int, out_path: str | Path) -> Path:
    """Public API: machine-readable versioned KPI sidecar."""
    return KpiJsonSidecar().write(kpis, schema_version, out_path)


__all__ = ["REPORT_FILE", "write_kpi_json", "write_run_report"]
