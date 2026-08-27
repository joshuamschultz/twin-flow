"""COMP-026 HtmlReport - one self-contained, offline HTML report.

Styled to the BlackArc brand: navy ink base, azure + cyan accents, orange for
attention, Space Grotesk display / Inter body / JetBrains Mono figures. Leads
with the Assumptions block (REQ-033), then headline KPI cards, charts, and detail
tables, with the reproducibility stamp in the footer.

Fully offline (D-026): Plotly's own JS is inlined ONCE, UNMODIFIED, inside a
delimited `<script id="twinflow-plotly-lib">` block - never scrubbed (scrubbing
the bundle's inert `cdn.plot.ly` topojson default breaks the library and blanks
every chart). None of our own markup loads an external asset; the bundle's
internal default URLs are only reached by geo/mapbox traces this report never
builds. Font stacks fall back to system fonts so nothing is fetched from a web
font host either.
"""

from __future__ import annotations

import html as html_lib
from pathlib import Path
from typing import Any

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import plotly.offline as pyo  # type: ignore[import-untyped]  # no stub package; D-026 lib choice

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report.assumptions import Assumption
from twinflow.run_stamp import RunStamp

# BlackArc brand tokens (navy ink base, azure/cyan accents, orange attention).
# Kept inline so the report is one self-contained file with no external stylesheet.
_STYLE = """
:root{
  --bg:#0B1220; --surface:#002550; --surface2:#001A38; --line:#1c3560;
  --text:#C7D6E8; --muted:#8FA6C4; --bright:#FFFFFF;
  --azure:#0073FE; --azure-300:#5A9CFF; --azure-100:#D6E6FF;
  --cyan:#22D3EE; --orange:#F68D2E;
  --fh:'Space Grotesk','Inter','Segoe UI',sans-serif;
  --fb:'Inter','system-ui','Helvetica Neue',Arial,sans-serif;
  --fm:'JetBrains Mono','SF Mono','Menlo','Consolas',monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--fb);line-height:1.55;}
.wrap{max-width:1120px;margin:0 auto;padding:34px 26px 72px;}
header.report{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:10px;}
header.report .kicker{font-family:var(--fm);color:var(--azure-300);letter-spacing:.22em;
  text-transform:uppercase;font-size:11px;}
header.report h1{font-family:var(--fh);color:var(--bright);font-weight:600;font-size:30px;
  margin:8px 0 4px;letter-spacing:.01em;}
header.report .sub{font-family:var(--fm);color:var(--muted);font-size:12.5px;}
h2{font-family:var(--fh);color:var(--azure-300);font-size:14px;letter-spacing:.14em;
  text-transform:uppercase;border-left:3px solid var(--azure);padding-left:11px;
  margin:38px 0 14px;font-weight:600;}
.cards{display:flex;flex-wrap:wrap;gap:14px;}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:15px 18px;min-width:150px;flex:1;}
.card .label{font-family:var(--fm);font-size:10.5px;color:var(--muted);
  text-transform:uppercase;letter-spacing:.12em;}
.card .value{font-family:var(--fh);font-size:27px;margin-top:7px;font-weight:600;
  color:var(--azure-300);text-shadow:0 0 12px rgba(90,156,255,.30);}
.card .value.cyan{color:var(--cyan);text-shadow:0 0 12px rgba(34,211,238,.30);}
.card .value.azure{color:var(--azure-300);text-shadow:0 0 12px rgba(90,156,255,.30);}
.card .value.orange{color:var(--orange);text-shadow:0 0 12px rgba(246,141,46,.28);}
.assumptions ul{list-style:none;padding:0;margin:0;}
.assumptions li{background:var(--surface);border:1px solid var(--line);
  border-left:3px solid var(--orange);border-radius:8px;padding:9px 13px;
  margin:8px 0;font-size:13.5px;}
.assumptions strong{font-family:var(--fm);color:var(--orange);}
.chart{background:var(--surface2);border:1px solid var(--line);border-radius:10px;
  padding:6px;margin:14px 0;}
.note{color:var(--muted);font-size:12px;margin:2px 0 16px;}
h3.tbl{font-family:var(--fm);color:var(--muted);font-size:12px;
  text-transform:uppercase;letter-spacing:.08em;margin:20px 0 6px;}
table{width:100%;border-collapse:collapse;font-size:13px;margin:4px 0 10px;
  background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--line);}
tr:last-child td{border-bottom:none;}
th{font-family:var(--fm);color:var(--muted);text-transform:uppercase;font-size:10.5px;
  letter-spacing:.09em;background:rgba(0,0,0,.20);}
td{font-family:var(--fm);color:var(--text);}
td.num,th.num{text-align:right;}
td.pos{color:var(--cyan);} td.neg{color:var(--orange);}
footer.stamp{margin-top:44px;border-top:1px solid var(--line);padding-top:16px;
  font-family:var(--fm);font-size:11px;color:var(--muted);}
footer.stamp b{color:var(--text);font-weight:500;}
"""

# Chart trace palette - blues + cyan + orange, no green (BlackArc brand).
_COLORWAY = ["#0073FE", "#22D3EE", "#F68D2E", "#5A9CFF", "#9CC2FF", "#3A6BA8", "#D6E6FF"]
_PLOT_BG = "rgba(0,37,80,0.45)"
_GRID = "#173056"
_AXIS_TEXT = "#8FA6C4"


def _e(text: object) -> str:
    """Escape element text content (apostrophes are fine in text, not attributes)."""
    return html_lib.escape(str(text), quote=False)


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
        first, before any KPI chart, so a reader sees what the engine substituted
        before trusting a single number.
        """
        path = Path(out_path)
        stamp = run_stamp.to_dict()
        body = [
            "<!DOCTYPE html>",
            "<html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>twinflow simulation report</title>",
            f"<style>{_STYLE}</style></head><body><div class='wrap'>",
            self._header(stamp),
            self._assumptions(assumptions),
            self._cards(kpis),
            self._charts(kpis),
            self._tables(kpis),
            self._footer(stamp),
            "</div></body></html>",
        ]
        path.write_text("\n".join(body), encoding="utf-8")
        return path

    # -- sections ----------------------------------------------------------

    @staticmethod
    def _header(stamp: dict[str, Any]) -> str:
        engine = _e(stamp.get("engine_version", "?"))
        seed = _e(stamp.get("base_seed", "?"))
        py = _e(stamp.get("python_version", "?"))
        return (
            "<header class='report'>"
            "<div class='kicker'>twinflow &middot; simulation report</div>"
            "<h1>Run results</h1>"
            f"<div class='sub'>engine {engine} &nbsp;&bull;&nbsp; seed {seed} "
            f"&nbsp;&bull;&nbsp; python {py}</div>"
            "</header>"
        )

    @staticmethod
    def _assumptions(assumptions: list[Assumption]) -> str:
        """Every substituted default, named verbatim (REQ-033); an empty list
        still renders the section."""
        if not assumptions:
            inner = "<p>No defaults were substituted; every value came from the model.</p>"
        else:
            rows = "".join(
                f"<li><strong>{_e(a.field)}</strong> &mdash; {_e(a.default_used)} "
                f"<span style='color:var(--muted)'>({_e(a.why_absent)})</span></li>"
                for a in assumptions
            )
            inner = f"<ul>{rows}</ul>"
        return (
            f"<section id='assumptions' class='assumptions'><h2>Assumptions</h2>{inner}</section>"
        )

    def _cards(self, kpis: KpiSet) -> str:
        machine_hours = sum(kpis.machine_hours_by_machine.values())
        n_orders = len(kpis.completion_by_order)
        completed = sum(1 for v in kpis.completion_by_order.values() if v is not None)
        cards = [
            ("On-time", f"{kpis.on_time_pct:.0f}%", "azure"),
            ("Orders completed", f"{completed}/{n_orders}", "cyan"),
            ("Run hours", f"{kpis.run_hours:.2f}", "azure"),
            ("Machine hours", f"{machine_hours:.2f}", "cyan"),
            ("Setup hours", f"{kpis.setup_hours:.2f}", "orange"),
            ("Work centers", f"{len(kpis.event_counts_by_location)}", "azure"),
        ]
        chips = "".join(
            f"<div class='card'><div class='label'>{_e(label)}</div>"
            f"<div class='value {cls}'>{_e(value)}</div></div>"
            for label, value, cls in cards
        )
        return f"<section id='summary'><h2>Headline</h2><div class='cards'>{chips}</div></section>"

    def _charts(self, kpis: KpiSet) -> str:
        """Charts + the single inlined Plotly library. The library goes in THIS
        section (after Assumptions) so the first `Plotly.newPlot(` in the file is
        never before the Assumptions block."""
        figures = [
            (self._lateness_figure(kpis), "Negative bars finish early; orange bars are late."),
            (
                self._utilization_figure(kpis),
                "Busy time as a share of the run - the tall bar is the bottleneck.",
            ),
            (
                self._wait_figure(kpis),
                "Where jobs waited, and why: no upstream work, blocked downstream, or waiting on material.",
            ),
            (self._wip_figure(kpis), "Jobs in progress at each work center over the run."),
        ]
        parts = []
        for fig, note in figures:
            chart = fig.to_html(full_html=False, include_plotlyjs=False)
            parts.append(f"<div class='chart'>{chart}</div><p class='note'>{note}</p>")
        return f"<section id='charts'><h2>Charts</h2>{self._plotly_library()}{''.join(parts)}</section>"

    def _tables(self, kpis: KpiSet) -> str:
        return (
            "<section id='detail'><h2>Detail</h2>"
            + self._orders_table(kpis)
            + self._utilization_table(kpis)
            + self._wait_table(kpis)
            + self._machine_hours_table(kpis)
            + self._labor_hours_table(kpis)
            + "</section>"
        )

    @staticmethod
    def _footer(stamp: dict[str, Any]) -> str:
        rows = " &nbsp;&bull;&nbsp; ".join(f"<b>{_e(k)}</b> {_e(v)}" for k, v in stamp.items())
        return (
            "<footer class='stamp'>Reproducibility stamp &mdash; this exact run "
            f"re-creates from: {rows}</footer>"
        )

    # -- tables ------------------------------------------------------------

    @staticmethod
    def _orders_table(kpis: KpiSet) -> str:
        rows = []
        for order in sorted(kpis.completion_by_order):
            completion = kpis.completion_by_order[order]
            lateness = kpis.lateness_by_order.get(order)
            comp_txt = "-" if completion is None else f"{completion:.1f}"
            if lateness is None:
                late_txt, cls = "-", "num"
            else:
                late_txt = f"{lateness:+.1f}"
                cls = "num pos" if lateness <= 0 else "num neg"
            rows.append(
                f"<tr><td>{_e(order)}</td><td class='num'>{comp_txt}</td>"
                f"<td class='{cls}'>{late_txt}</td></tr>"
            )
        return (
            "<h3 class='tbl'>Completion &amp; lateness by order</h3>"
            "<table><tr><th>Order</th><th class='num'>Completion (s)</th>"
            "<th class='num'>Lateness (s)</th></tr>" + "".join(rows) + "</table>"
        )

    @staticmethod
    def _utilization_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(cell)}</td><td class='num'>{util * 100:.1f}%</td></tr>"
            for cell, util in sorted(kpis.utilization_by_cell.items())
        )
        return (
            "<h3 class='tbl'>Utilization by work center</h3>"
            "<table><tr><th>Work center</th><th class='num'>Utilization</th></tr>"
            + rows
            + "</table>"
        )

    @staticmethod
    def _wait_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(loc)}</td>"
            f"<td class='num'>{w.get('starved', 0.0):.0f}</td>"
            f"<td class='num'>{w.get('blocked', 0.0):.0f}</td>"
            f"<td class='num'>{w.get('material_starved', 0.0):.0f}</td></tr>"
            for loc, w in sorted(kpis.wait_seconds_by_location.items())
        )
        return (
            "<h3 class='tbl'>Wait breakdown by location (seconds)</h3>"
            "<table><tr><th>Location</th><th class='num'>Starved</th>"
            "<th class='num'>Blocked</th><th class='num'>Material-starved</th></tr>"
            + rows
            + "</table>"
        )

    @staticmethod
    def _machine_hours_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(machine)}</td><td class='num'>{hours:.3f}</td></tr>"
            for machine, hours in sorted(kpis.machine_hours_by_machine.items())
        )
        return (
            "<h3 class='tbl'>Machine hours</h3>"
            "<table><tr><th>Machine</th><th class='num'>Hours</th></tr>" + rows + "</table>"
        )

    @staticmethod
    def _labor_hours_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(pool)}</td><td>{_e(skill)}</td><td class='num'>{hours:.3f}</td></tr>"
            for (pool, skill), hours in sorted(kpis.labor_hours_by_pool_skill.items())
        )
        return (
            "<h3 class='tbl'>Labor hours by pool and skill</h3>"
            "<table><tr><th>Pool</th><th>Skill</th><th class='num'>Hours</th></tr>"
            + rows
            + "</table>"
        )

    # -- figures -----------------------------------------------------------

    @staticmethod
    def _plotly_library() -> str:
        """Plotly's own bundled JS, inlined UNMODIFIED in one delimited script tag
        (D-026: fully offline). Never scrubbed - scrubbing the bundle's inert
        `cdn.plot.ly` default corrupts the library and blanks every chart."""
        return f"<script id='twinflow-plotly-lib'>{pyo.get_plotlyjs()}</script>"

    @staticmethod
    def _style(fig: go.Figure, title: str, yaxis_title: str, xaxis_title: str = "") -> go.Figure:
        fig.update_layout(
            title={"text": title, "font": {"family": "Space Grotesk, sans-serif", "size": 15}},
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor=_PLOT_BG,
            font={"family": "JetBrains Mono, monospace", "color": _AXIS_TEXT, "size": 12},
            colorway=_COLORWAY,
            margin={"l": 60, "r": 24, "t": 48, "b": 48},
            height=340,
            xaxis_title=xaxis_title,
            yaxis_title=yaxis_title,
            legend={"bgcolor": "rgba(0,0,0,0)"},
        )
        fig.update_xaxes(gridcolor=_GRID, zerolinecolor="#2e4c82")
        fig.update_yaxes(gridcolor=_GRID, zerolinecolor="#2e4c82")
        return fig

    def _lateness_figure(self, kpis: KpiSet) -> go.Figure:
        orders = sorted(kpis.lateness_by_order)
        lateness = [kpis.lateness_by_order[o] for o in orders]
        colors = ["#0073FE" if (v is not None and v <= 0) else "#F68D2E" for v in lateness]
        fig = go.Figure(go.Bar(x=orders, y=lateness, marker_color=colors, name="Lateness"))
        return self._style(fig, "Lateness by order", "Signed lateness (s)", "Order")

    def _utilization_figure(self, kpis: KpiSet) -> go.Figure:
        cells = sorted(kpis.utilization_by_cell)
        util = [kpis.utilization_by_cell[c] * 100 for c in cells]
        fig = go.Figure(go.Bar(x=cells, y=util, marker_color="#0073FE", name="Utilization"))
        return self._style(fig, "Utilization by work center", "Utilization (%)", "Work center")

    def _wait_figure(self, kpis: KpiSet) -> go.Figure:
        locs = sorted(kpis.wait_seconds_by_location)
        fig = go.Figure()
        for key, color in (
            ("starved", "#5A9CFF"),
            ("blocked", "#F68D2E"),
            ("material_starved", "#22D3EE"),
        ):
            fig.add_trace(
                go.Bar(
                    x=locs,
                    y=[kpis.wait_seconds_by_location[loc].get(key, 0.0) for loc in locs],
                    name=key.replace("_", "-"),
                    marker_color=color,
                )
            )
        fig.update_layout(barmode="stack")
        return self._style(fig, "Wait breakdown by location", "Wait (s)", "Location")

    def _wip_figure(self, kpis: KpiSet) -> go.Figure:
        """WIP over time as clean STEP lines (no markers): WIP holds a level until
        the next entry/exit, so a step read is honest and far less cluttered than
        dot-to-dot markers over hundreds of sweep points."""
        fig = go.Figure()
        wip = kpis.wip_by_location
        for location_id in wip["location_id"].unique(maintain_order=True).to_list():
            series = wip.filter(wip["location_id"] == location_id).sort("t")
            fig.add_trace(
                go.Scatter(
                    x=series["t"].to_list(),
                    y=series["wip"].to_list(),
                    mode="lines",
                    line={"shape": "hv", "width": 1.6},
                    name=str(location_id),
                )
            )
        return self._style(fig, "WIP over time", "Jobs in progress", "Sim time (s)")
