"""Layer 4 — self-contained HTML + versioned KPI JSON sidecar + Assumptions block."""

from __future__ import annotations


def render_html(kpis: object, assumptions: list[object], run_stamp: object, out_path: str) -> str:
    """Public API: one offline HTML file, Assumptions block first."""
    raise NotImplementedError("T-045")


def write_kpi_json(kpis: object, schema_version: int, out_path: str) -> str:
    """Public API: machine-readable versioned KPI sidecar."""
    raise NotImplementedError("T-045")
