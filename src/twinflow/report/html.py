"""COMP-026 HtmlReport - one self-contained, shareable, offline HTML report.

Light, print-quality layout in the BlackArc brand (navy header/section bars, azure
accents, ink text on white, Space Grotesk display / Inter body / JetBrains Mono
micro-labels), lots of white space and a responsive grid. Leads with the Assumptions
block (REQ-033), a metadata strip, headline KPI cards, then a rendered value-stream
diagram of the material flow, charts with plain-language explainers, and detail tables,
closing with the reproducibility stamp.

Fully offline (D-026): Plotly and Mermaid are both inlined once, UNMODIFIED, in
delimited `<script>` blocks - never scrubbed. The value-stream diagram is generated as
Mermaid from the compiled model and rendered in-browser by the vendored Mermaid bundle
(`_assets/mermaid.min.js`, which sets `globalThis.mermaid`). None of our own markup
loads an external asset; font stacks fall back to system fonts.
"""

from __future__ import annotations

import base64
import html as html_lib
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import plotly.offline as pyo  # type: ignore[import-untyped]  # no stub package; D-026 lib choice

from twinflow.instrumentation.kpis import KpiSet
from twinflow.report.assumptions import Assumption
from twinflow.run_stamp import RunStamp

if TYPE_CHECKING:
    from twinflow.instrumentation.aggregate import AggregatedKpis, Interval
    from twinflow.model import CompiledModel

_MERMAID_JS = Path(__file__).parent / "_assets" / "mermaid.min.js"
_LOGO_PNG = Path(__file__).parent / "_assets" / "logo.png"


def _logo_data_uri() -> str:
    """The brand emblem inlined as a base64 PNG data URI (offline-safe)."""
    return "data:image/png;base64," + base64.b64encode(_LOGO_PNG.read_bytes()).decode("ascii")


def _error_y(intervals: list[Interval], scale: float = 1.0) -> dict[str, Any]:
    """A Plotly asymmetric error-bar spec (mean->hi up, mean->lo down) for a row
    of confidence intervals, optionally rescaled (e.g. fractions to percent)."""
    return {
        "type": "data",
        "symmetric": False,
        "array": [(iv.hi - iv.mean) * scale for iv in intervals],
        "arrayminus": [(iv.mean - iv.lo) * scale for iv in intervals],
        "color": "#002550",
        "thickness": 1.4,
    }


def _fig_html(fig: go.Figure) -> str:
    """One Plotly figure as a responsive, full-width, library-free HTML fragment."""
    return fig.to_html(  # type: ignore[no-any-return]
        full_html=False,
        include_plotlyjs=False,
        default_width="100%",
        config={"responsive": True},
    )

# BlackArc light brand tokens (navy on white, azure accents) - see the brand standard.
_STYLE = """
:root{
  --navy:#002550; --ink:#0B1220; --navy900:#001A38;
  --azure:#0073FE; --azure-300:#5A9CFF; --azure-50:#EEF4FF; --cyan:#0891B2; --orange:#F68D2E;
  --paper:#FFFFFF; --panel:#F6F8FB; --line:#E4EAF2; --line2:#D3E0F0;
  --text:#1B2A41; --muted:#5A6B85; --faint:#8598B4;
  --fh:'Space Grotesk','Inter','Segoe UI',sans-serif;
  --fb:'Inter','system-ui','Helvetica Neue',Arial,sans-serif;
  --fm:'JetBrains Mono','SF Mono','Menlo','Consolas',monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--panel);color:var(--text);font-family:var(--fb);
  line-height:1.55;-webkit-font-smoothing:antialiased;}
.wrap{max-width:1160px;margin:0 auto;padding:0 0 64px;}
/* header bar */
header.report{background:var(--navy);color:#fff;padding:26px 34px;
  display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:16px;}
header .brand{display:flex;align-items:center;gap:15px;}
header .brand .logo{height:52px;width:52px;border-radius:12px;display:block;
  box-shadow:0 2px 10px rgba(0,0,0,.28);}
header .brand .word{font-family:var(--fh);font-weight:700;font-size:27px;letter-spacing:.01em;}
header .brand .word .accent{color:var(--azure-300);}
header .brand .tag{font-family:var(--fm);font-size:10.5px;letter-spacing:.28em;
  text-transform:uppercase;color:var(--azure-300);margin-top:2px;}
header .title{text-align:right;}
header .title h1{font-family:var(--fh);font-weight:600;font-size:22px;margin:0;letter-spacing:.14em;
  text-transform:uppercase;}
header .title .sub{font-family:var(--fm);font-size:11.5px;color:var(--azure-300);margin-top:3px;}
.body{padding:0 34px;}
/* metadata strip */
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-top:none;margin-bottom:30px;}
.meta .cell{background:var(--paper);padding:12px 16px;}
.meta .cell .k{font-family:var(--fm);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--faint);}
.meta .cell .v{font-family:var(--fm);font-size:14px;color:var(--text);margin-top:4px;}
/* section headers */
h2.sec{font-family:var(--fh);font-size:12px;letter-spacing:.16em;text-transform:uppercase;
  color:#fff;background:var(--navy);padding:8px 14px;margin:34px 0 4px;border-radius:5px;}
p.explain{color:var(--muted);font-size:13px;margin:6px 2px 14px;max-width:760px;}
/* KPI cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;}
.card{background:var(--paper);border:1px solid var(--line);border-top:3px solid var(--azure);
  border-radius:9px;padding:14px 16px;}
.card.warn{border-top-color:var(--orange);}
.card .label{font-family:var(--fm);font-size:10px;color:var(--faint);text-transform:uppercase;
  letter-spacing:.12em;}
.card .value{font-family:var(--fh);font-size:26px;font-weight:600;color:var(--navy);margin-top:6px;}
.card .value.azure{color:var(--azure);} .card .value.orange{color:var(--orange);}
.card .foot{font-family:var(--fm);font-size:11px;color:var(--muted);margin-top:2px;}
/* assumptions */
.assumptions ul{list-style:none;padding:0;margin:0;
  display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:10px;}
.assumptions li{background:var(--paper);border:1px solid var(--line);
  border-left:3px solid var(--orange);
  border-radius:8px;padding:10px 13px;font-size:13px;}
.assumptions strong{font-family:var(--fm);color:var(--orange);}
/* diagram + charts grid */
.panel{background:var(--paper);border:1px solid var(--line);border-radius:11px;
  padding:12px 12px 4px;min-width:0;}
.flow{background:var(--paper);border:1px solid var(--line);border-radius:11px;
  padding:18px;
  overflow-x:auto;}
.flow .mermaid{display:flex;justify-content:center;}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:16px;}
.chart .cap{font-family:var(--fb);color:var(--muted);font-size:12px;padding:2px 6px 10px;}
/* tables */
.tables{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;align-items:start;}
h3.tbl{font-family:var(--fm);color:var(--muted);font-size:11px;text-transform:uppercase;
  letter-spacing:.08em;margin:0 0 6px;}
table{width:100%;border-collapse:collapse;font-size:12.5px;background:var(--paper);
  border:1px solid var(--line);border-radius:9px;overflow:hidden;}
th,td{text-align:left;padding:7px 11px;border-bottom:1px solid var(--line);}
tr:last-child td{border-bottom:none;} tr:nth-child(even) td{background:#FBFCFE;}
th{font-family:var(--fm);color:var(--faint);text-transform:uppercase;font-size:9.5px;
  letter-spacing:.08em;background:var(--azure-50);}
td{font-family:var(--fm);color:var(--text);}
td.num,th.num{text-align:right;}
td.pos{color:var(--cyan);} td.neg{color:var(--orange);}
footer.stamp{margin:40px 34px 0;border-top:1px solid var(--line);padding-top:16px;
  font-family:var(--fm);font-size:11px;color:var(--muted);}
footer.stamp b{color:var(--text);font-weight:500;}
"""

_COLORWAY = ["#0073FE", "#0891B2", "#F68D2E", "#5A9CFF", "#3A6BA8", "#9CC2FF", "#002550"]


def _e(text: object) -> str:
    """Escape element text content."""
    return html_lib.escape(str(text), quote=False)


def _nid(raw: str) -> str:
    """A safe Mermaid node id from an arbitrary name."""
    return "n_" + re.sub(r"[^0-9A-Za-z_]", "_", raw)


class HtmlReport:
    """Renders `KpiSet` (+ optional model) into one offline, shareable HTML report."""

    def render(
        self,
        kpis: KpiSet,
        assumptions: list[Assumption],
        run_stamp: RunStamp,
        out_path: str | Path,
        model: CompiledModel | None = None,
        aggregated: AggregatedKpis | None = None,
    ) -> Path:
        """Write one self-contained HTML report to `out_path` and return it. The
        Assumptions block comes first, before any KPI chart (REQ-033)."""
        path = Path(out_path)
        stamp = run_stamp.to_dict()
        body = [
            "<!DOCTYPE html>",
            "<html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>twinflow simulation report</title>",
            f"<style>{_STYLE}</style>",
            self._plotly_library(),
            "</head><body><div class='wrap'>",
            self._header(),
            "<div class='body'>",
            self._meta(kpis, stamp),
            self._assumptions(assumptions),
            self._cards(kpis, aggregated),
            self._confidence_section(aggregated),
            self._flow_section(kpis, model),
            self._charts(kpis),
            self._tables(kpis),
            "</div>",
            self._footer(stamp),
            "</div>",
            self._mermaid_library(),
            self._plotly_resize_script(),
            "</body></html>",
        ]
        path.write_text("\n".join(body), encoding="utf-8")
        return path

    # -- header / meta -----------------------------------------------------

    @staticmethod
    def _header() -> str:
        return (
            "<header class='report'>"
            "<div class='brand'>"
            f"<img class='logo' alt='twinflow' src='{_logo_data_uri()}'>"
            "<div class='wordmark'><div class='word'>twin<span class='accent'>flow</span></div>"
            "<div class='tag'>digital-twin simulator</div></div></div>"
            "<div class='title'><h1>Simulation Report</h1>"
            "<div class='sub'>one run &middot; reproducible</div></div>"
            "</header>"
        )

    def _meta(self, kpis: KpiSet, stamp: dict[str, Any]) -> str:
        cells = [
            ("Engine", stamp.get("engine_version", "?")),
            ("Seed", stamp.get("base_seed", "?")),
            ("Python", stamp.get("python_version", "?")),
            ("Work centers", len(kpis.event_counts_by_location)),
            ("Orders", len(kpis.completion_by_order)),
            ("On-time", f"{kpis.on_time_pct:.0f}%"),
        ]
        inner = "".join(
            f"<div class='cell'><div class='k'>{_e(k)}</div><div class='v'>{_e(v)}</div></div>"
            for k, v in cells
        )
        return f"<div class='meta'>{inner}</div>"

    @staticmethod
    def _assumptions(assumptions: list[Assumption]) -> str:
        if not assumptions:
            inner = "<p>No defaults were substituted; every value came from the model.</p>"
        else:
            rows = "".join(
                f"<li><strong>{_e(a.field)}</strong> &mdash; {_e(a.default_used)} "
                f"<span style='color:var(--faint)'>({_e(a.why_absent)})</span></li>"
                for a in assumptions
            )
            inner = f"<ul>{rows}</ul>"
        return (
            "<section id='assumptions' class='assumptions'>"
            "<h2 class='sec'>Assumptions</h2>"
            "<p class='explain'>Every value the engine had to substitute for something the "
            "model did not specify. Read these first - they frame every number below.</p>"
            f"{inner}</section>"
        )

    def _cards(self, kpis: KpiSet, aggregated: AggregatedKpis | None = None) -> str:
        machine_hours = sum(kpis.machine_hours_by_machine.values())
        n_orders = len(kpis.completion_by_order)
        completed = sum(1 for v in kpis.completion_by_order.values() if v is not None)
        late = sum(1 for v in kpis.lateness_by_order.values() if v is not None and v > 0)
        bottleneck, bn_util = self._bottleneck(kpis)
        if aggregated is not None and aggregated.reps > 1:
            iv = aggregated.on_time_pct
            on_time_value = f"{iv.mean:.0f}%"
            on_time_foot = f"range {iv.lo:.0f}-{iv.hi:.0f}% over {aggregated.reps} runs"
        else:
            on_time_value = f"{kpis.on_time_pct:.0f}%"
            on_time_foot = f"{late} of {n_orders} late"
        cards = [
            ("On-time", on_time_value, "azure", on_time_foot, False),
            ("Orders completed", f"{completed}/{n_orders}", "", "reached a finished part", False),
            ("Bottleneck", bottleneck or "-", "orange", f"{bn_util * 100:.0f}% utilized", True),
            ("Run length", f"{kpis.run_hours:.2f}h", "", "total machine hours", False),
            ("Machine hours", f"{machine_hours:.2f}", "", "summed across centers", False),
            ("Work centers", f"{len(kpis.event_counts_by_location)}", "", "in the flow", False),
        ]
        chips = "".join(
            f"<div class='card{' warn' if warn else ''}'><div class='label'>{_e(label)}</div>"
            f"<div class='value {cls}'>{_e(value)}</div><div class='foot'>{_e(foot)}</div></div>"
            for label, value, cls, foot, warn in cards
        )
        return f"<section><h2 class='sec'>Headline</h2><div class='cards'>{chips}</div></section>"

    # -- confidence ranges across replications -----------------------------

    def _confidence_section(self, aggregated: AggregatedKpis | None) -> str:
        """Range charts across replications. Empty for a single run (no spread
        to report) so a one-off render is unchanged."""
        if aggregated is None or aggregated.reps <= 1:
            return ""
        note = (
            f"Across {aggregated.reps} replications, reported as a "
            f"{aggregated.level * 100:.0f}% band. The bar is the mean; the whisker is the "
            "low-to-high range a real, variable floor produces - the honest answer, not a "
            "single number pretending to be certain."
        )
        panels = "".join(
            f"<div class='panel chart'>{_fig_html(fig)}<div class='cap'>{_e(cap)}</div></div>"
            for fig, cap in (
                (self._lateness_range_figure(aggregated),
                 "Lateness per order: negative is early, positive is late. A whisker "
                 "crossing zero is an order that ships on time in some runs, late in others."),
                (self._utilization_range_figure(aggregated),
                 "Busy share per work center. A tall bar with a tight whisker is a "
                 "dependable bottleneck; a wide whisker is a center whose load swings."),
            )
        )
        return (
            f"<section><h2 class='sec'>Confidence ranges</h2>"
            f"<p class='cap'>{_e(note)}</p><div class='grid2'>{panels}</div></section>"
        )

    def _lateness_range_figure(self, aggregated: AggregatedKpis) -> go.Figure:
        orders = sorted(aggregated.lateness_by_order)
        ivs = [aggregated.lateness_by_order[o] for o in orders]
        means = [iv.mean for iv in ivs]
        fig = go.Figure(go.Bar(x=orders, y=means, marker_color="#0073FE", error_y=_error_y(ivs)))
        return self._style(fig, "Lateness by order (range)", "Lateness (s)", "Order")

    def _utilization_range_figure(self, aggregated: AggregatedKpis) -> go.Figure:
        by_cell = aggregated.utilization_by_cell
        cells = sorted(by_cell, key=lambda c: by_cell[c].mean, reverse=True)
        ivs = [by_cell[c] for c in cells]
        means = [iv.mean * 100.0 for iv in ivs]
        fig = go.Figure(
            go.Bar(x=cells, y=means, marker_color="#0891B2", error_y=_error_y(ivs, scale=100.0))
        )
        return self._style(fig, "Utilization by center (range)", "Utilization (%)", "Work center")

    # -- material flow (value-stream) diagram ------------------------------

    def _flow_section(self, kpis: KpiSet, model: CompiledModel | None) -> str:
        if model is None:
            return ""
        diagram = self._flow_diagram(kpis, model)
        return (
            "<section><h2 class='sec'>Material flow</h2>"
            "<p class='explain'>The floor as declared in the model: stocks (cylinders) feed "
            "work centers (boxes) along the routing to a finished part (dark). Dashed arrows "
            "return scrap or rework to a stock. Each center is annotated with its cycle time, "
            "utilization and peak work-in-process from this run - a value-stream view. The "
            "amber box is the busiest center.</p>"
            f"<div class='flow'><pre class='mermaid'>{diagram}</pre></div></section>"
        )

    def _flow_diagram(self, kpis: KpiSet, model: CompiledModel) -> str:
        bottleneck, _ = self._bottleneck(kpis)
        wip_peak = self._wip_peak_by_location(kpis)
        stock_names = {s.name for s in model.stocks}

        lines = ["flowchart LR"]
        for stock in model.stocks:
            lines.append(f'  {_nid(stock.name)}[("{_e(stock.name)}")]:::stock')

        for loc in model.locations:
            loc_id = loc.location_id
            util = kpis.utilization_by_cell.get(loc_id, 0.0) * 100
            firings = kpis.event_counts_by_location.get(loc_id, 0)
            hours = kpis.machine_hours_by_machine.get(loc_id, 0.0)
            cycle = (hours * 3600.0 / firings) if firings else 0.0
            label = (
                f"<b>{_e(loc_id)}</b><br/>util {util:.0f}% · {cycle:.0f}s/job"
                f"<br/>wip peak {wip_peak.get(loc_id, 0)}"
            )
            cls = "bott" if loc_id == bottleneck else "wc"
            lines.append(f'  {_nid(loc_id)}["{label}"]:::{cls}')
            # incoming stock feeds (a consumed thing that is a declared stock)
            for thing in loc.pull_rule.setup_key_of:
                if thing in stock_names:
                    lines.append(f"  {_nid(thing)} --> {_nid(loc_id)}")
            # scrap / rework returned to a stock
            for thing, stock_name in loc.stock_destinations.items():
                lines.append(f"  {_nid(loc_id)} -.->|{_e(thing)}| {_nid(stock_name)}")

        for part, steps in model.routing.items():
            for a, b in zip(steps, steps[1:], strict=False):
                lines.append(f"  {_nid(a)} --> {_nid(b)}")
            if steps:
                fin = _nid("_fin_" + part)
                lines.append(f'  {fin}(["{_e(part)}"]):::fin')
                lines.append(f"  {_nid(steps[-1])} --> {fin}")

        lines += [
            "classDef stock fill:#EEF4FF,stroke:#0073FE,color:#0B1220;",
            "classDef wc fill:#FFFFFF,stroke:#3A6BA8,color:#0B1220;",
            "classDef bott fill:#FFF3E6,stroke:#F68D2E,color:#0B1220,stroke-width:2px;",
            "classDef fin fill:#002550,stroke:#001A38,color:#FFFFFF;",
        ]
        return "\n".join(lines)

    # -- charts ------------------------------------------------------------

    def _charts(self, kpis: KpiSet) -> str:
        specs = [
            (self._throughput_figure(kpis), "Cumulative orders finished over the run - a flat "
             "stretch means nothing completed while work piled up behind the constraint."),
            (self._utilization_figure(kpis), "Busy share of the run per work center. The tall "
             "bar is the bottleneck; everything else has headroom."),
            (self._cycle_time_figure(kpis), "Average processing time per job at each center - "
             "the slow step is usually the constraint."),
            (self._wait_figure(kpis), "How each center spent the run: busy vs blocked (finished "
             "but held) vs starved (idle, waiting for work). The bottleneck runs busy; the "
             "centers it feeds starve."),
            (self._lateness_figure(kpis), "Signed lateness per order - blue finished early, "
             "orange finished late."),
            (self._wip_figure(kpis), "Jobs in progress at each center over time - a rising line "
             "is a growing queue."),
        ]
        panels = "".join(
            f"<div class='panel chart'>{_fig_html(fig)}"
            f"<div class='cap'>{cap}</div></div>"
            for fig, cap in specs
        )
        return f"<section><h2 class='sec'>Charts</h2><div class='grid2'>{panels}</div></section>"

    def _tables(self, kpis: KpiSet) -> str:
        return (
            "<section><h2 class='sec'>Detail</h2><div class='tables'>"
            + self._orders_table(kpis)
            + self._utilization_table(kpis)
            + self._wait_table(kpis)
            + self._machine_hours_table(kpis)
            + self._labor_hours_table(kpis)
            + "</div></section>"
        )

    @staticmethod
    def _footer(stamp: dict[str, Any]) -> str:
        rows = " &nbsp;&bull;&nbsp; ".join(f"<b>{_e(k)}</b> {_e(v)}" for k, v in stamp.items())
        return (
            "<footer class='stamp'>Reproducibility stamp &mdash; this exact run re-creates "
            f"from: {rows}</footer>"
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _bottleneck(kpis: KpiSet) -> tuple[str | None, float]:
        if not kpis.utilization_by_cell:
            return None, 0.0
        loc = max(kpis.utilization_by_cell, key=lambda k: kpis.utilization_by_cell[k])
        return loc, kpis.utilization_by_cell[loc]

    @staticmethod
    def _wip_peak_by_location(kpis: KpiSet) -> dict[str, int]:
        wip = kpis.wip_by_location
        if wip.height == 0:
            return {}
        peaks = wip.group_by("location_id").agg(pl_max_wip())
        return {str(row["location_id"]): int(row["wip"]) for row in peaks.iter_rows(named=True)}

    # -- tables ------------------------------------------------------------

    @staticmethod
    def _orders_table(kpis: KpiSet) -> str:
        rows = []
        for order in sorted(kpis.completion_by_order):
            completion = kpis.completion_by_order[order]
            lateness = kpis.lateness_by_order.get(order)
            comp_txt = "-" if completion is None else f"{completion:.0f}"
            if lateness is None:
                late_txt, cls = "-", "num"
            else:
                late_txt = f"{lateness:+.0f}"
                cls = "num pos" if lateness <= 0 else "num neg"
            rows.append(
                f"<tr><td>{_e(order)}</td><td class='num'>{comp_txt}</td>"
                f"<td class='{cls}'>{late_txt}</td></tr>"
            )
        return (
            "<div><h3 class='tbl'>Completion &amp; lateness by order</h3>"
            "<table><tr><th>Order</th><th class='num'>Done (s)</th>"
            "<th class='num'>Late (s)</th></tr>" + "".join(rows) + "</table></div>"
        )

    @staticmethod
    def _utilization_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(cell)}</td><td class='num'>{min(util, 1.0) * 100:.1f}%</td></tr>"
            for cell, util in sorted(
                kpis.utilization_by_cell.items(), key=lambda kv: -kv[1]
            )
        )
        return (
            "<div><h3 class='tbl'>Utilization by work center</h3>"
            "<table><tr><th>Work center</th><th class='num'>Utilization</th></tr>"
            + rows
            + "</table></div>"
        )

    @staticmethod
    def _wait_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(loc)}</td>"
            f"<td class='num'>{w.get('starved', 0.0):.0f}</td>"
            f"<td class='num'>{w.get('blocked', 0.0):.0f}</td></tr>"
            for loc, w in sorted(kpis.wait_seconds_by_location.items())
        )
        return (
            "<div><h3 class='tbl'>Idle time by center (s)</h3>"
            "<table><tr><th>Location</th><th class='num'>Starved</th>"
            "<th class='num'>Blocked</th></tr>" + rows + "</table></div>"
        )

    @staticmethod
    def _machine_hours_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(machine)}</td><td class='num'>{hours:.3f}</td></tr>"
            for machine, hours in sorted(kpis.machine_hours_by_machine.items())
        )
        return (
            "<div><h3 class='tbl'>Machine hours</h3>"
            "<table><tr><th>Machine</th><th class='num'>Hours</th></tr>" + rows + "</table></div>"
        )

    @staticmethod
    def _labor_hours_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(pool)}</td><td>{_e(skill)}</td><td class='num'>{hours:.3f}</td></tr>"
            for (pool, skill), hours in sorted(kpis.labor_hours_by_pool_skill.items())
        )
        return (
            "<div><h3 class='tbl'>Labor hours by pool &amp; skill</h3>"
            "<table><tr><th>Pool</th><th>Skill</th><th class='num'>Hours</th></tr>"
            + rows
            + "</table></div>"
        )

    # -- figures -----------------------------------------------------------

    @staticmethod
    def _plotly_resize_script() -> str:
        """Re-fit every chart once the grid has settled (initial draw happens
        mid-parse, before the two-column layout exists, so charts otherwise
        keep the full-width size they first measured)."""
        js = (
            "window.addEventListener('load',function(){"
            "if(!window.Plotly)return;"
            "document.querySelectorAll('.plotly-graph-div')"
            ".forEach(function(d){window.Plotly.Plots.resize(d);});});"
        )
        return f"<script>{js}</script>"

    @staticmethod
    def _plotly_library() -> str:
        return f"<script id='twinflow-plotly-lib'>{pyo.get_plotlyjs()}</script>"

    @staticmethod
    def _mermaid_library() -> str:
        js = _MERMAID_JS.read_text(encoding="utf-8")
        init = (
            "mermaid.initialize({startOnLoad:true,theme:'base',"
            "themeVariables:{fontFamily:'JetBrains Mono, monospace',fontSize:'13px',"
            "primaryColor:'#EEF4FF',primaryBorderColor:'#3A6BA8',primaryTextColor:'#0B1220',"
            "lineColor:'#5A6B85'}});"
        )
        return f"<script id='twinflow-mermaid-lib'>{js}</script><script>{init}</script>"

    @staticmethod
    def _style(fig: go.Figure, title: str, yaxis_title: str, xaxis_title: str = "") -> go.Figure:
        fig.update_layout(
            title={"text": title, "font": {"family": "Space Grotesk, sans-serif", "size": 15,
                                            "color": "#0B1220"}},
            template="plotly_white",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="#FFFFFF",
            font={"family": "JetBrains Mono, monospace", "color": "#5A6B85", "size": 12},
            colorway=_COLORWAY,
            margin={"l": 58, "r": 20, "t": 46, "b": 44},
            height=320,
            autosize=True,
            xaxis_title=xaxis_title,
            yaxis_title=yaxis_title,
            showlegend=False,
        )
        fig.update_xaxes(gridcolor="#EEF2F8", linecolor="#D3E0F0")
        fig.update_yaxes(gridcolor="#EEF2F8", linecolor="#D3E0F0")
        return fig

    def _throughput_figure(self, kpis: KpiSet) -> go.Figure:
        times = sorted(v for v in kpis.completion_by_order.values() if v is not None)
        xs = [0.0, *times]
        ys = list(range(len(xs)))
        fig = go.Figure(
            go.Scatter(
                x=xs, y=ys, mode="lines",
                line={"shape": "hv", "color": "#0073FE", "width": 2},
            )
        )
        return self._style(fig, "Cumulative completions", "Orders finished", "Sim time (s)")

    def _utilization_figure(self, kpis: KpiSet) -> go.Figure:
        items = sorted(kpis.utilization_by_cell.items(), key=lambda kv: kv[1])
        cells = [c for c, _ in items]
        util = [min(u, 1.0) * 100 for _, u in items]
        top = max(range(len(util)), default=-1, key=lambda i: util[i]) if util else -1
        colors = ["#F68D2E" if i == top else "#0073FE" for i in range(len(util))]
        fig = go.Figure(go.Bar(x=util, y=cells, orientation="h", marker_color=colors))
        return self._style(fig, "Utilization by work center", "", "Utilization (%)")

    def _cycle_time_figure(self, kpis: KpiSet) -> go.Figure:
        cells, cyc = [], []
        for loc in sorted(kpis.machine_hours_by_machine):
            firings = kpis.event_counts_by_location.get(loc, 0)
            hours = kpis.machine_hours_by_machine[loc]
            cells.append(loc)
            cyc.append((hours * 3600.0 / firings) if firings else 0.0)
        fig = go.Figure(go.Bar(x=cells, y=cyc, marker_color="#0891B2"))
        return self._style(fig, "Cycle time by stage", "Seconds / job", "Work center")

    def _wait_figure(self, kpis: KpiSet) -> go.Figure:
        locs = sorted(kpis.wait_seconds_by_location)
        fig = go.Figure()
        for key, color, name in (
            ("starved", "#5A9CFF", "starved"),
            ("blocked", "#F68D2E", "blocked"),
        ):
            fig.add_trace(
                go.Bar(
                    x=locs,
                    y=[kpis.wait_seconds_by_location[loc].get(key, 0.0) for loc in locs],
                    name=name,
                    marker_color=color,
                )
            )
        fig.update_layout(barmode="stack", showlegend=True,
                          legend={"orientation": "h", "y": 1.12, "x": 0})
        return self._style(fig, "Idle time by center", "Idle (s)", "Location")

    def _lateness_figure(self, kpis: KpiSet) -> go.Figure:
        orders = sorted(kpis.lateness_by_order)
        lateness = [kpis.lateness_by_order[o] for o in orders]
        colors = ["#0073FE" if (v is not None and v <= 0) else "#F68D2E" for v in lateness]
        fig = go.Figure(go.Bar(x=orders, y=lateness, marker_color=colors))
        return self._style(fig, "Lateness by order", "Signed lateness (s)", "Order")

    def _wip_figure(self, kpis: KpiSet) -> go.Figure:
        fig = go.Figure()
        wip = kpis.wip_by_location
        for i, location_id in enumerate(wip["location_id"].unique(maintain_order=True).to_list()):
            series = wip.filter(wip["location_id"] == location_id).sort("t")
            fig.add_trace(
                go.Scatter(
                    x=series["t"].to_list(),
                    y=series["wip"].to_list(),
                    mode="lines",
                    line={"shape": "hv", "width": 1.6, "color": _COLORWAY[i % len(_COLORWAY)]},
                    name=str(location_id),
                )
            )
        fig.update_layout(showlegend=True, legend={"orientation": "h", "y": 1.12, "x": 0})
        return self._style(fig, "WIP over time", "Jobs in progress", "Sim time (s)")


def pl_max_wip() -> Any:
    """`pl.col("wip").max()` as a small named helper (keeps the import local)."""
    import polars as pl

    return pl.col("wip").max().alias("wip")
