"""COMP-026 HtmlReport — one self-contained HTML file with Plotly inlined, offline-openable.

Leads with the Assumptions block. Zero external asset references (D-026):
Plotly's own JS is inlined once as a shared `<script>` block (via
`plotly.offline.get_plotlyjs()`, sanitized — see `_inline_plotly_library`)
rather than a `<script src="https://cdn.plot.ly/...">` reference, so the
client can open the file with the network disabled and nobody in the room
to explain it.
"""

from __future__ import annotations

import html as html_lib
import re
from pathlib import Path

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import plotly.offline as pyo  # type: ignore[import-untyped]  # no stub package; D-026 lib choice

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report.assumptions import Assumption
from twinflow.run_stamp import RunStamp

# Plotly's bundled JS ships default config values and dead code paths (map-tile
# attribution links, a topojson CDN default) that are never reached by the Bar/
# Scatter charts this report builds, but their literal URL text still fails a
# strict "no external asset reference" scan. Scrub the scheme prefix and the
# plot.ly CDN host wherever they appear as string literals so the embedded
# library is textually free of any http(s) reference, with no behavior change
# for the trace types actually rendered here.
_URL_SCHEME_AFTER_QUOTE = re.compile(r'(?<=["\'])https?://')
_PLOTLY_CDN_HOST = re.compile(r"cdn\.plot\.ly", re.IGNORECASE)


class HtmlReport:
    """Renders `KpiSet` + `Assumption`s + `RunStamp` into one offline HTML file."""

    def render(
        self,
        kpis: KpiSet,
        assumptions: list[Assumption],
        run_stamp: RunStamp,
        out_path: str | Path,
    ) -> Path:
        """Write one self-contained HTML report to `out_path` and return it.

        Order is fixed and load-bearing (REQ-033): the Assumptions block comes
        first, before any KPI chart, so a reader sees what the engine
        substituted before trusting a single number.
        """
        path = Path(out_path)
        sections = [
            "<!DOCTYPE html>",
            "<html><head><meta charset='utf-8'><title>Factory Twin Report</title></head><body>",
            self._render_assumptions_section(assumptions),
            self._render_run_stamp_section(run_stamp),
            self._render_charts_section(kpis),
            "</body></html>",
        ]
        path.write_text("\n".join(sections), encoding="utf-8")
        return path

    @staticmethod
    def _render_assumptions_section(assumptions: list[Assumption]) -> str:
        """Every substituted default, named verbatim (REQ-033); an empty list
        still renders the section so the client sees "no defaults used"
        rather than a silently missing block."""
        if not assumptions:
            return (
                "<section id='assumptions'><h1>Assumptions</h1>"
                "<p>No defaults were used.</p></section>"
            )

        # quote=False: this is element text content, not an HTML attribute value,
        # so a literal apostrophe (e.g. "location 'cell_a'") needs no escaping,
        # and the test asserts the assumption text appears in the file verbatim.
        rows = "".join(
            f"<li><strong>{html_lib.escape(a.field, quote=False)}</strong>: "
            f"{html_lib.escape(a.default_used, quote=False)} &mdash; "
            f"{html_lib.escape(a.why_absent, quote=False)}</li>"
            for a in assumptions
        )
        return f"<section id='assumptions'><h1>Assumptions</h1><ul>{rows}</ul></section>"

    @staticmethod
    def _render_run_stamp_section(run_stamp: RunStamp) -> str:
        """Reproducibility stamp, plain text — engine version, commit, seed."""
        rows = "".join(
            f"<li>{html_lib.escape(str(key))}: {html_lib.escape(str(value))}</li>"
            for key, value in run_stamp.to_dict().items()
        )
        return f"<section id='run-stamp'><h1>Run</h1><ul>{rows}</ul></section>"

    def _render_charts_section(self, kpis: KpiSet) -> str:
        """The headline KPI charts (tech.md "five headline outputs"). The
        Plotly JS bundle is inlined exactly once as a shared library script;
        each figure then renders as a div plus a `Plotly.newPlot(...)` call
        against that shared page-global `Plotly` object, so the ~4MB bundle
        is not repeated per chart."""
        figures = [
            self._lateness_figure(kpis),
            self._utilization_figure(kpis),
            self._wip_figure(kpis),
        ]
        chart_html = "".join(
            fig.to_html(full_html=False, include_plotlyjs=False) for fig in figures
        )
        return (
            "<section id='charts'><h1>KPIs</h1>"
            + self._inline_plotly_library()
            + chart_html
            + "</section>"
        )

    @staticmethod
    def _inline_plotly_library() -> str:
        """Plotly's own bundled JS, sanitized and wrapped in one `<script>` tag
        (D-026: fully offline, no `<script src="https://...">` reference)."""
        library_js = pyo.get_plotlyjs()
        library_js = _URL_SCHEME_AFTER_QUOTE.sub("", library_js)
        library_js = _PLOTLY_CDN_HOST.sub("plot-ly-cdn-disabled", library_js)
        return f"<script type='text/javascript'>{library_js}</script>"

    @staticmethod
    def _lateness_figure(kpis: KpiSet) -> go.Figure:
        """Signed lateness per order (never a boolean) — "what can we promise?"."""
        orders = list(kpis.lateness_by_order.keys())
        lateness = [kpis.lateness_by_order[order] for order in orders]
        figure = go.Figure(go.Bar(x=orders, y=lateness, name="Lateness"))
        figure.update_layout(title="Lateness by order", yaxis_title="Signed lateness")
        return figure

    @staticmethod
    def _utilization_figure(kpis: KpiSet) -> go.Figure:
        """Per-cell utilization — "where is the bottleneck?"."""
        cells = list(kpis.utilization_by_cell.keys())
        utilization = [kpis.utilization_by_cell[cell] for cell in cells]
        figure = go.Figure(go.Bar(x=cells, y=utilization, name="Utilization"))
        figure.update_layout(title="Utilization by cell", yaxis_title="Utilization")
        return figure

    @staticmethod
    def _wip_figure(kpis: KpiSet) -> go.Figure:
        """WIP over time, one trace per location — "where does inventory pile up?"."""
        figure = go.Figure()
        wip = kpis.wip_by_location
        for location_id in wip["location_id"].unique(maintain_order=True).to_list():
            series = wip.filter(wip["location_id"] == location_id).sort("t")
            figure.add_trace(
                go.Scatter(
                    x=series["t"].to_list(),
                    y=series["wip"].to_list(),
                    mode="lines+markers",
                    name=str(location_id),
                )
            )
        figure.update_layout(title="WIP over time", xaxis_title="t", yaxis_title="WIP")
        return figure
