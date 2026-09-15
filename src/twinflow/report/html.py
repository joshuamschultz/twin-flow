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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import plotly.offline as pyo  # type: ignore[import-untyped]  # no stub package; D-026 lib choice

from twinflow.instrumentation.kpis import KpiSet
from twinflow.primitives.calendar import AlwaysWorkingCalendar, WorkingCalendar
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
/* The flow lives in a tall, clipped stage the reader zooms and drags inside,
   instead of a squeezed unreadable strip. The SVG keeps its natural size and is
   moved by a CSS transform (see _flow_zoom_script). */
.flow{background:var(--paper);border:1px solid var(--line);border-radius:11px;
  padding:18px;height:78vh;min-height:520px;overflow:hidden;position:relative;
  cursor:grab;}
.flow .mermaid{display:block;}
/* The rendered SVG is sized to its natural viewBox in px by renderFlow (an
   inline-block/width:auto SVG collapses to 0x0 inside this stage). max-width
   must not clamp it back, so the reader can pan the full-size diagram. */
.flow .mermaid svg{max-width:none!important;}
.flow-controls{display:flex;align-items:center;gap:6px;margin:0 0 8px;}
.flow-controls button{font-family:var(--fm);font-size:12px;border:1px solid var(--line);
  background:var(--paper);color:var(--text);border-radius:7px;padding:3px 10px;cursor:pointer;}
.flow-controls button:hover{background:var(--azure-50);}
.flow-controls .hint{color:var(--faint);font-family:var(--fm);font-size:10.5px;
  margin-left:6px;letter-spacing:.04em;}
/* Tab bar */
.tabs{display:flex;gap:2px;margin:0 34px;border-bottom:1px solid var(--line);
  flex-wrap:wrap;}
.tabs .tab{font-family:var(--fm);font-size:11px;letter-spacing:.10em;
  text-transform:uppercase;color:var(--muted);background:none;border:none;
  border-bottom:2px solid transparent;padding:11px 15px;cursor:pointer;}
.tabs .tab:hover{color:var(--text);}
.tabs .tab.active{color:var(--azure);border-bottom-color:var(--azure);}
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
td.mono{font-family:var(--fm);} td.muted{color:var(--muted);font-size:11.5px;}
table.tbl caption{caption-side:top;text-align:left;font-family:var(--fm);color:var(--muted);
  font-size:11px;text-transform:uppercase;letter-spacing:.08em;padding:0 0 6px;}
.ubar{position:relative;height:15px;border-radius:7px;background:var(--azure-50);
  min-width:120px;overflow:hidden;}
.ubar span{position:absolute;left:0;top:0;bottom:0;border-radius:7px;}
.ubar em{position:absolute;right:6px;top:0;line-height:15px;font-style:normal;
  font-family:var(--fm);font-size:10.5px;color:var(--text);}
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


def _origin(model: CompiledModel | None) -> datetime | None:
    """The model's wall-clock start instant (simulation second 0), read from the
    first labor pool that declares a shift calendar. `None` when the model has no
    calendar — the report then falls back to raw simulation seconds."""
    if model is None:
        return None
    for pool in model.labor_pools:
        calendar = getattr(pool, "calendar", None)
        origin = getattr(calendar, "origin", None)
        if isinstance(origin, datetime):
            return origin
    return None


def _fmt_time(seconds: float | None, origin: datetime | None) -> str:
    """Format a simulation time. With an `origin` a raw second count becomes a
    real local date-time (`Thu Sep 17 09:14`), because "266400 s" means nothing
    to a reader; without one it stays `266400 s`."""
    if seconds is None:
        return "-"
    if origin is None:
        return f"{seconds:.0f} s"
    return (origin + timedelta(seconds=seconds)).strftime("%a %b %d %H:%M")


def _fmt_lateness(seconds: float) -> str:
    """Signed lateness as hours (`+2.6 h` late, `-8.0 h` early) — hours read far
    faster than a five-digit second count for a human scanning the table."""
    return f"{seconds / 3600.0:+.1f} h"


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
        horizon: float | None = None,
    ) -> Path:
        """Write one self-contained HTML report to `out_path` and return it. The
        Assumptions block comes first, before any KPI chart (REQ-033). `horizon`
        (the run's length in seconds) lets the report report utilization of the
        SHARED physical resources — one row per machine pool and labor pool —
        rather than only per-routing-step."""
        path = Path(out_path)
        stamp = run_stamp.to_dict()
        origin = _origin(model)
        body = [
            "<!DOCTYPE html>",
            "<html lang='en'><head><meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>twinflow simulation report</title>",
            f"<style>{_STYLE}</style>",
            self._plotly_library(),
            "</head><body><div class='wrap'>",
            self._header(),
            self._meta(kpis, stamp),
            self._tabs_nav(bool(model)),
            "<div class='body'>",
            # Overview tab: the headline KPIs, confidence ranges, shared resources.
            "<div class='panel-tab' data-panel='overview'>",
            self._cards(kpis, aggregated),
            self._confidence_section(aggregated),
            self._resources_section(kpis, model, horizon),
            "</div>",
            # Charts tab.
            "<div class='panel-tab' data-panel='charts' hidden>",
            self._charts(kpis, origin),
            "</div>",
            # Flow tab (only when a model was supplied).
            "<div class='panel-tab' data-panel='flow' hidden>",
            self._flow_section(kpis, model),
            "</div>",
            # Detail tab: the raw tables.
            "<div class='panel-tab' data-panel='detail' hidden>",
            self._tables(kpis, origin),
            "</div>",
            # Assumptions tab: off the main view, summarized.
            "<div class='panel-tab' data-panel='assumptions' hidden>",
            self._assumptions(assumptions),
            "</div>",
            "</div>",
            self._footer(stamp),
            "</div>",
            self._mermaid_library(),
            self._plotly_resize_script(),
            self._tabs_script(),
            self._flow_zoom_script(),
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

    @staticmethod
    def _tabs_nav(has_model: bool) -> str:
        """A tab bar so the report reads as pages, not one long scroll. The
        Assumptions block, which balloons to one card per work center, gets its
        own tab instead of monopolizing the top of the report."""
        tabs = [("overview", "Overview"), ("charts", "Charts")]
        if has_model:
            tabs.append(("flow", "Material flow"))
        tabs += [("detail", "Detail"), ("assumptions", "Assumptions")]
        buttons = "".join(
            f"<button class='tab{' active' if i == 0 else ''}' "
            f"data-tab='{key}'>{_e(label)}</button>"
            for i, (key, label) in enumerate(tabs)
        )
        return f"<nav class='tabs'>{buttons}</nav>"

    @staticmethod
    def _tabs_script() -> str:
        """Plain-DOM tab switching. On activation, fire a resize so any Plotly
        chart that was hidden (zero-width) at load lays itself out correctly."""
        return (
            "<script>(function(){"
            "var tabs=document.querySelectorAll('.tab');"
            "var panels=document.querySelectorAll('.panel-tab');"
            "var flowDone=false;"
            # A mermaid SVG carries width='100%' and only a viewBox, so inside the
            # inline stage it renders 0x0. Pin its natural viewBox size in px once
            # rendered, so it shows at full size and the reader can pan/zoom it.
            "function sizeFlowSvg(pre){var s=pre&&pre.querySelector('svg');if(!s)return;"
            "var vb=(s.getAttribute('viewBox')||'').split(/\\s+/);"
            "if(vb.length===4){s.style.width=vb[2]+'px';s.style.height=vb[3]+'px';}"
            "s.removeAttribute('width');s.removeAttribute('height');}"
            "function renderFlow(){"
            "if(flowDone||typeof mermaid==='undefined')return;"
            "var pre=document.querySelector('.panel-tab[data-panel=\"flow\"] pre.mermaid');"
            "if(!pre)return;"
            "flowDone=true;"
            "try{Promise.resolve(mermaid.run({nodes:[pre]}))"
            ".then(function(){sizeFlowSvg(pre);})"
            ".catch(function(){try{mermaid.init(undefined,pre);sizeFlowSvg(pre);}catch(_){}});}"
            "catch(e){try{mermaid.init(undefined,pre);sizeFlowSvg(pre);}catch(_){}}"
            "}"
            "function show(name){"
            "panels.forEach(function(p){p.hidden=(p.dataset.panel!==name);});"
            "tabs.forEach(function(t){t.classList.toggle('active',t.dataset.tab===name);});"
            "if(name==='flow')renderFlow();"
            "window.dispatchEvent(new Event('resize'));"
            "}"
            "tabs.forEach(function(t){t.addEventListener('click',function(){"
            "show(t.dataset.tab);});});"
            "})();</script>"
        )

    @staticmethod
    def _flow_zoom_script() -> str:
        """Zoom (+/-/reset buttons and ctrl+wheel) and pan (drag) for the flow
        diagram's SVG, using a CSS transform. No external library; the container
        clips and the SVG moves inside it."""
        return (
            "<script>(function(){"
            "var box=document.getElementById('flow-stage');"
            "if(!box)return;"
            "var scale=1,tx=0,ty=0,drag=false,px=0,py=0;"
            "function art(){return box.querySelector('svg');}"
            "function apply(){var s=art();if(s){"
            "s.style.transformOrigin='0 0';"
            "s.style.transform='translate('+tx+'px,'+ty+'px) scale('+scale+')';}}"
            "function zoom(f){scale=Math.min(6,Math.max(0.2,scale*f));apply();}"
            "document.querySelectorAll('[data-flow-zoom]').forEach(function(b){"
            "b.addEventListener('click',function(){var a=b.dataset.flowZoom;"
            "if(a==='in')zoom(1.25);else if(a==='out')zoom(0.8);"
            "else{scale=1;tx=0;ty=0;apply();}});});"
            "box.addEventListener('wheel',function(e){if(!e.ctrlKey)return;"
            "e.preventDefault();zoom(e.deltaY<0?1.1:0.9);},{passive:false});"
            "box.addEventListener('mousedown',function(e){drag=true;px=e.clientX;py=e.clientY;"
            "box.style.cursor='grabbing';});"
            "window.addEventListener('mouseup',function(){drag=false;box.style.cursor='grab';});"
            "window.addEventListener('mousemove',function(e){if(!drag)return;"
            "tx+=e.clientX-px;ty+=e.clientY-py;px=e.clientX;py=e.clientY;apply();});"
            "})();</script>"
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
            # Group identical (default_used, why-pattern) assumptions so that "no
            # pull rule declared for location X" over 23 locations becomes ONE row
            # naming the count and listing the locations, not 23 near-identical
            # cards. The location name is the varying tail after "for location".
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            for a in assumptions:
                subject = a.field.split(".", 1)[1] if "." in a.field else a.field
                pattern = re.sub(r"\bfor location '.*?'", "for a location", a.why_absent)
                key = (a.default_used, pattern)
                entry = groups.setdefault(key, {"subjects": [], "whys": []})
                entry["subjects"].append(subject)
                entry["whys"].append(a.why_absent)
            body_rows = []
            for (default_used, pattern), entry in sorted(groups.items()):
                subjects = entry["subjects"]
                count = len(subjects)
                # A single instance keeps its exact wording; only genuine
                # repetition (the same default across many locations) collapses
                # to the pattern, so no real detail is lost.
                why = entry["whys"][0] if count == 1 else pattern
                where = _e(", ".join(sorted(subjects)))
                body_rows.append(
                    f"<tr><td class='num'>{count}</td>"
                    f"<td class='mono'>{_e(default_used)}</td>"
                    f"<td class='muted'>{_e(why)}</td>"
                    f"<td class='muted'>{where}</td></tr>"
                )
            inner = (
                f"<p class='explain'>{len(assumptions)} default(s) substituted, "
                f"grouped into {len(groups)} kind(s) below.</p>"
                "<table class='tbl'><thead><tr><th class='num'>count</th>"
                "<th>default used</th><th>why</th><th>where</th></tr></thead>"
                f"<tbody>{''.join(body_rows)}</tbody></table>"
            )
        return (
            "<section id='assumptions' class='assumptions'>"
            "<h2 class='sec'>Assumptions</h2>"
            "<p class='explain'>Every value the engine had to substitute for something the "
            "model did not specify. These frame every number in the report.</p>"
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
                (
                    self._lateness_range_figure(aggregated),
                    "Lateness per order: negative is early, positive is late. A whisker "
                    "crossing zero is an order that ships on time in some runs, late in others.",
                ),
                (
                    self._utilization_range_figure(aggregated),
                    "Busy share per work center. A tall bar with a tight whisker is a "
                    "dependable bottleneck; a wide whisker is a center whose load swings.",
                ),
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

    # -- shared physical resources -----------------------------------------

    @staticmethod
    def _operating_hours_by_pool(model: CompiledModel, horizon: float) -> dict[str, float]:
        """Operating (on-shift) hours in [0, horizon] for each labor pool, from its
        declared shift calendar. A pool with no calendar is open 24/7, so its
        operating hours equal the whole run. This is the TRUE-utilization
        denominator: a resource's busy time measured against the time it is
        actually open, not the round-the-clock wall clock."""
        result: dict[str, float] = {}
        for pool in model.labor_pools:
            calendar = getattr(pool, "calendar", None)
            cal = WorkingCalendar(calendar) if calendar is not None else AlwaysWorkingCalendar()
            result[pool.name] = cal.available_seconds(horizon) / 3600.0
        return result

    @staticmethod
    def _shared_resource_rows(
        kpis: KpiSet, model: CompiledModel, horizon: float
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Utilization of the SHARED physical resources, aggregated to the real
        thing — one row per machine pool, one per labor pool — not per routing
        step. A floor with one fridge shared by three bread chains reports one
        `fridge_m` utilization, not three; a floor with one baker reports one
        `bakers` utilization, the number that says whether there is room for more
        work.

        Each row carries TWO utilizations: `util_operating` (busy over the hours
        the resource is actually OPEN, from the staffing pool's shift calendar —
        the honest "how slammed am I on the clock?" number) and `util_wall`
        (busy over the full 24/7 run, which a buffer that holds product overnight
        legitimately fills). Returns (machine_rows, labor_rows)."""
        horizon_h = horizon / 3600.0 if horizon else 0.0
        operating_by_pool = HtmlReport._operating_hours_by_pool(model, horizon)
        pool_by_skill = {skill: pool.name for pool in model.labor_pools for skill in pool.skills}

        def _util(busy_h: float, denom_h: float, capacity: int) -> float:
            return busy_h / (denom_h * capacity) if denom_h and capacity else 0.0

        machines: dict[str, dict[str, Any]] = {}
        for loc in model.locations:
            machine_id = loc.machine.machine_id
            entry = machines.setdefault(
                machine_id,
                {
                    "capacity": getattr(loc, "capacity", 1),
                    "steps": [],
                    "busy_h": 0.0,
                    "skill": getattr(loc, "labor_skill", None),
                },
            )
            entry["steps"].append(loc.location_id)
            entry["busy_h"] += kpis.busy_hours_by_location.get(loc.location_id, 0.0)
        machine_rows = []
        for machine_id, entry in sorted(machines.items()):
            pool_name = pool_by_skill.get(entry["skill"])
            operating_h = operating_by_pool.get(pool_name, horizon_h) if pool_name else horizon_h
            machine_rows.append(
                {
                    "resource": machine_id,
                    "capacity": entry["capacity"],
                    "steps": entry["steps"],
                    "busy_hours": entry["busy_h"],
                    "operating_hours": operating_h,
                    "util_operating": _util(entry["busy_h"], operating_h, entry["capacity"]),
                    "util_wall": _util(entry["busy_h"], horizon_h, entry["capacity"]),
                }
            )

        labor_rows = []
        for pool in model.labor_pools:
            busy_h = sum(
                hours
                for (pool_name, _skill), hours in kpis.labor_hours_by_pool_skill.items()
                if pool_name == pool.name
            )
            headcount = getattr(pool, "headcount", 1) or 1
            operating_h = operating_by_pool.get(pool.name, horizon_h)
            labor_rows.append(
                {
                    "resource": pool.name,
                    "capacity": headcount,
                    "skills": sorted(pool.skills),
                    "busy_hours": busy_h,
                    "operating_hours": operating_h,
                    "util_operating": _util(busy_h, operating_h, headcount),
                    "util_wall": _util(busy_h, horizon_h, headcount),
                }
            )
        return machine_rows, labor_rows

    def _resources_section(
        self, kpis: KpiSet, model: CompiledModel | None, horizon: float | None
    ) -> str:
        if model is None or not horizon:
            return ""
        machine_rows, labor_rows = self._shared_resource_rows(kpis, model, horizon)

        def util_bar(pct: float) -> str:
            width = max(0.0, min(100.0, pct))
            hot = "#F68D2E" if pct >= 85 else "#0073FE"
            return (
                f"<div class='ubar'><span style='width:{width:.0f}%;background:{hot}'></span>"
                f"<em>{pct:.0f}%</em></div>"
            )

        def machine_rows_html(rows: list[dict[str, Any]]) -> str:
            # Machines report OCCUPANCY over the full 24/7 run: a fridge holds
            # loaves overnight while the plant is closed, so "open hours" is the
            # wrong denominator (it would read over 100%). 24/7 occupancy is
            # always honest and never exceeds a slot's own time.
            out = []
            for r in rows:
                detail = ", ".join(str(s) for s in r["steps"])
                out.append(
                    f"<tr><td class='mono'>{_e(r['resource'])}</td>"
                    f"<td class='num'>{r['capacity']}</td>"
                    f"<td>{util_bar(r['util_wall'] * 100)}</td>"
                    f"<td class='num'>{r['busy_hours']:.1f}</td>"
                    f"<td class='muted'>{_e(detail)}</td></tr>"
                )
            return "".join(out)

        def labor_rows_html(rows: list[dict[str, Any]]) -> str:
            # Labor reports TRUE utilization over on-shift hours (the bar), with
            # 24/7 alongside. Labor time only accrues while actually working, so
            # this denominator is well defined and <= 100%.
            out = []
            for r in rows:
                detail = ", ".join(str(s) for s in r["skills"])
                out.append(
                    f"<tr><td class='mono'>{_e(r['resource'])}</td>"
                    f"<td class='num'>{r['capacity']}</td>"
                    f"<td>{util_bar(r['util_operating'] * 100)}</td>"
                    f"<td class='num muted'>{r['util_wall'] * 100:.0f}%</td>"
                    f"<td class='num'>{r['busy_hours']:.1f}</td>"
                    f"<td class='muted'>{_e(detail)}</td></tr>"
                )
            return "".join(out)

        horizon_h = horizon / 3600.0
        # The plant's operating window, taken from the busiest (most-constrained)
        # labor pool's calendar, expressed as days + hours the way a plant runs.
        operating_h = max((r["operating_hours"] for r in labor_rows), default=horizon_h)
        open_pct = (operating_h / horizon_h * 100.0) if horizon_h else 0.0
        window = (
            f"Operating window: <b>{operating_h:.0f} h</b> open over a "
            f"{horizon_h / 24.0:.1f}-day run "
            f"({operating_h / 24.0:.1f} days, {open_pct:.0f}% of the clock). "
            "Labor utilization (the bar) is measured over those OPEN hours &mdash; "
            "the true “how slammed is the baker” number; the grey "
            "<b>24/7</b> column is the same over the full clock. Machine occupancy "
            "is over 24/7, because a buffer holds product even while the plant is "
            "closed."
        )

        return (
            "<section><h2 class='sec'>Shared resources</h2>"
            "<p class='explain'>Utilization of the real, shared equipment and "
            "labor &mdash; one row per machine pool and per labor pool, not per "
            "routing step. When several steps share one machine (a single fridge "
            "used by every bread), this is the one number that says whether that "
            "resource has room for more work. A bar near 100% is a true "
            "constraint; a low bar has spare capacity, which for an occasional "
            "step (an oven that only runs when dough is ready) is expected, not "
            "waste.</p>"
            f"<p class='explain'>{window}</p>"
            "<div class='tables'>"
            "<table class='tbl'><caption>Labor pools (utilization over open hours)"
            "</caption><thead><tr>"
            "<th>pool</th><th class='num'>people</th><th>util (on shift)</th>"
            "<th class='num'>24/7</th><th class='num'>busy&nbsp;h</th>"
            "<th>skills</th></tr></thead>"
            f"<tbody>{labor_rows_html(labor_rows)}</tbody></table>"
            "<table class='tbl'><caption>Machine pools (occupancy over 24/7)"
            "</caption><thead><tr>"
            "<th>machine</th><th class='num'>slots</th><th>occupancy</th>"
            "<th class='num'>busy&nbsp;h</th>"
            "<th>steps sharing it</th></tr></thead>"
            f"<tbody>{machine_rows_html(machine_rows)}</tbody></table>"
            "</div></section>"
        )

    # -- material flow (value-stream) diagram ------------------------------

    def _flow_section(self, kpis: KpiSet, model: CompiledModel | None) -> str:
        if model is None:
            return ""
        diagram = self._flow_diagram(kpis, model)
        return (
            "<section><h2 class='sec'>Material flow</h2>"
            "<p class='explain'>The floor as declared in the model: stocks (cylinders) feed "
            "work centers (boxes) top-to-bottom along the routing to a finished part (dark). "
            "Dashed arrows return scrap or rework to a stock. Each center is annotated with "
            "its cycle time, utilization and peak work-in-process from this run - a "
            "value-stream view. The amber box is the busiest center.</p>"
            "<div class='flow-controls'>"
            "<button data-flow-zoom='out' title='zoom out'>&minus;</button>"
            "<button data-flow-zoom='in' title='zoom in'>+</button>"
            "<button data-flow-zoom='reset' title='reset'>reset</button>"
            "<span class='hint'>drag to pan &middot; ctrl + scroll to zoom</span>"
            "</div>"
            "<div class='flow' id='flow-stage'>"
            f"<pre class='mermaid'>{diagram}</pre></div></section>"
        )

    def _flow_diagram(self, kpis: KpiSet, model: CompiledModel) -> str:
        bottleneck, _ = self._bottleneck(kpis)
        wip_peak = self._wip_peak_by_location(kpis)
        stock_names = {s.name for s in model.stocks}

        lines = ["flowchart TB"]
        for stock in model.stocks:
            lines.append(f'  {_nid(stock.name)}[("{_e(stock.name)}")]:::stock')

        for loc in model.locations:
            loc_id = loc.location_id
            capacity = getattr(loc, "capacity", 1)
            util = kpis.utilization_by_cell.get(loc_id, 0.0) * 100
            firings = kpis.event_counts_by_location.get(loc_id, 0)
            hours = kpis.busy_hours_by_location.get(loc_id, 0.0)
            cycle = (hours * 3600.0 / firings) if firings else 0.0
            cls = "bott" if loc_id == bottleneck else "wc"
            # One box per work center, ALWAYS. A parallel bank is annotated with
            # "xN slots", never drawn as N separate boxes: a capacity-32 tub as
            # 32 nodes turns the whole diagram into hundreds of boxes and makes it
            # unreadable (COMP-030 parallelism is a number, not N shapes).
            slots = f" · x{capacity} slots" if capacity > 1 else ""
            label = (
                f"<b>{_e(loc_id)}</b>{slots}<br/>util {util:.0f}% · {cycle:.0f}s/job"
                f"<br/>wip peak {wip_peak.get(loc_id, 0)}"
            )
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
            "classDef mach fill:#FFFFFF,stroke:#5A9CFF,color:#0B1220;",
            "classDef bott fill:#FFF3E6,stroke:#F68D2E,color:#0B1220,stroke-width:2px;",
            "classDef fin fill:#002550,stroke:#001A38,color:#FFFFFF;",
        ]
        return "\n".join(lines)

    # -- charts ------------------------------------------------------------

    def _charts(self, kpis: KpiSet, origin: datetime | None = None) -> str:
        due_axis = "real dates" if origin is not None else "simulation seconds"
        specs = [
            (
                self._completion_timeline_figure(kpis, origin),
                "Every order on one timeline, in "
                f"{due_axis}: the bar runs from its due date to when it actually "
                "finished. Blue finished on or before the promise; orange finished "
                "late, and the bar length is how late. The diamond marks the due date.",
            ),
            (
                self._throughput_figure(kpis),
                "Cumulative orders finished over the run - a flat "
                "stretch means nothing completed while work piled up behind the constraint.",
            ),
            (
                self._utilization_figure(kpis),
                "Busy share of the run per work center. The tall "
                "bar is the bottleneck; everything else has headroom.",
            ),
            (
                self._cycle_time_figure(kpis),
                "Average processing time per job at each center - "
                "the slow step is usually the constraint.",
            ),
            (
                self._wait_figure(kpis),
                "How each center spent the run: busy vs blocked (finished "
                "but held) vs starved (idle, waiting for work). The bottleneck runs busy; the "
                "centers it feeds starve.",
            ),
            (
                self._lateness_figure(kpis),
                "Signed lateness per order - blue finished early, orange finished late.",
            ),
            (
                self._wip_figure(kpis),
                "Jobs in progress at each center over time - a rising line is a growing queue.",
            ),
        ]
        panels = "".join(
            f"<div class='panel chart'>{_fig_html(fig)}<div class='cap'>{cap}</div></div>"
            for fig, cap in specs
        )
        return f"<section><h2 class='sec'>Charts</h2><div class='grid2'>{panels}</div></section>"

    def _tables(self, kpis: KpiSet, origin: datetime | None = None) -> str:
        return (
            "<section><h2 class='sec'>Detail</h2><div class='tables'>"
            + self._orders_table(kpis, origin)
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
    def _orders_table(kpis: KpiSet, origin: datetime | None = None) -> str:
        rows = []
        for order in sorted(kpis.completion_by_order):
            completion = kpis.completion_by_order[order]
            lateness = kpis.lateness_by_order.get(order)
            comp_txt = _fmt_time(completion, origin)
            if lateness is None:
                late_txt, cls = "-", "num"
            else:
                late_txt = _fmt_lateness(lateness)
                cls = "num pos" if lateness <= 0 else "num neg"
            rows.append(
                f"<tr><td>{_e(order)}</td><td class='num'>{comp_txt}</td>"
                f"<td class='{cls}'>{late_txt}</td></tr>"
            )
        done_header = "Done" if origin is not None else "Done (s)"
        late_header = "Late" if origin is not None else "Late (s)"
        return (
            "<div><h3 class='tbl'>Completion &amp; lateness by order</h3>"
            f"<table><tr><th>Order</th><th class='num'>{done_header}</th>"
            f"<th class='num'>{late_header}</th></tr>" + "".join(rows) + "</table></div>"
        )

    @staticmethod
    def _utilization_table(kpis: KpiSet) -> str:
        rows = "".join(
            f"<tr><td>{_e(cell)}</td><td class='num'>{min(util, 1.0) * 100:.1f}%</td></tr>"
            for cell, util in sorted(kpis.utilization_by_cell.items(), key=lambda kv: -kv[1])
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
        # startOnLoad is OFF: the flow diagram lives in a tab that is hidden at
        # load, and mermaid renders nothing measurable inside a display:none box.
        # The tab script calls mermaid.run() the first time the Flow tab opens.
        init = (
            "mermaid.initialize({startOnLoad:false,theme:'base',"
            "themeVariables:{fontFamily:'JetBrains Mono, monospace',fontSize:'13px',"
            "primaryColor:'#EEF4FF',primaryBorderColor:'#3A6BA8',primaryTextColor:'#0B1220',"
            "lineColor:'#5A6B85'}});"
        )
        return f"<script id='twinflow-mermaid-lib'>{js}</script><script>{init}</script>"

    @staticmethod
    def _style(fig: go.Figure, title: str, yaxis_title: str, xaxis_title: str = "") -> go.Figure:
        fig.update_layout(
            title={
                "text": title,
                "font": {"family": "Space Grotesk, sans-serif", "size": 15, "color": "#0B1220"},
            },
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

    def _completion_timeline_figure(
        self, kpis: KpiSet, origin: datetime | None = None
    ) -> go.Figure:
        """A per-order Gantt-lite: one row per order, a bar from its DUE date to
        its actual COMPLETION, on a real-date axis when the model has a calendar.
        Blue = finished on time, orange = finished late (bar length = how late),
        a diamond at the due date. Answers 'did every promise land?' at a glance."""

        def _x(seconds: float) -> Any:
            return origin + timedelta(seconds=seconds) if origin is not None else seconds

        # due = completion - lateness (lateness is signed completion-minus-due).
        rows = []
        for order, completion in kpis.completion_by_order.items():
            lateness = kpis.lateness_by_order.get(order)
            if completion is None or lateness is None:
                due = None if completion is None else completion
            else:
                due = completion - lateness
            rows.append((order, completion, due, lateness))
        # sort by due date, then completion, so the two drop clusters read top-down
        rows.sort(
            key=lambda r: (
                r[2] if r[2] is not None else float("inf"),
                r[3] if r[3] is not None else float("inf"),
            )
        )
        orders = [r[0] for r in rows]

        on_x: list[Any] = []
        on_y: list[Any] = []
        late_x: list[Any] = []
        late_y: list[Any] = []
        for order, completion, due, lateness in rows:
            if completion is None or due is None:
                continue
            seg_x, seg_y = (on_x, on_y) if (lateness or 0) <= 0 else (late_x, late_y)
            seg_x.extend([_x(due), _x(completion), None])
            seg_y.extend([order, order, None])

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=on_x,
                y=on_y,
                mode="lines",
                line={"color": "#0073FE", "width": 7},
                name="on time",
                connectgaps=False,
            )
        )
        fig.add_trace(
            go.Scatter(
                x=late_x,
                y=late_y,
                mode="lines",
                line={"color": "#F68D2E", "width": 7},
                name="late",
                connectgaps=False,
            )
        )
        # due-date diamonds and completion dots
        due_pts = [(o, d) for o, _c, d, _l in rows if d is not None]
        comp_pts = [(o, c) for o, c, _d, _l in rows if c is not None]
        if due_pts:
            fig.add_trace(
                go.Scatter(
                    x=[_x(d) for _o, d in due_pts],
                    y=[o for o, _d in due_pts],
                    mode="markers",
                    marker={"symbol": "diamond", "size": 7, "color": "#5A6B85"},
                    name="due",
                )
            )
        if comp_pts:
            fig.add_trace(
                go.Scatter(
                    x=[_x(c) for _o, c in comp_pts],
                    y=[o for o, _c in comp_pts],
                    mode="markers",
                    marker={"symbol": "circle", "size": 6, "color": "#0B1220"},
                    name="finished",
                )
            )
        fig = self._style(
            fig,
            "Order timeline (due → finished)",
            "",
            "Finish time" if origin is not None else "Sim time (s)",
        )
        fig.update_layout(
            showlegend=True,
            legend={"orientation": "h", "y": 1.12, "x": 0},
            height=max(320, 16 * len(orders) + 90),
        )
        fig.update_yaxes(autorange="reversed", type="category")
        return fig

    def _throughput_figure(self, kpis: KpiSet) -> go.Figure:
        times = sorted(v for v in kpis.completion_by_order.values() if v is not None)
        xs = [0.0, *times]
        ys = list(range(len(xs)))
        fig = go.Figure(
            go.Scatter(
                x=xs,
                y=ys,
                mode="lines",
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
        # Busy hours are keyed by LOCATION (kpis.busy_hours_by_location), unlike
        # machine_hours_by_machine, which keys by physical machine instance once
        # resource attribution is on and would leave every per-stage bar at zero.
        cells, cyc = [], []
        for loc in sorted(kpis.busy_hours_by_location):
            firings = kpis.event_counts_by_location.get(loc, 0)
            hours = kpis.busy_hours_by_location[loc]
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
        fig.update_layout(
            barmode="stack", showlegend=True, legend={"orientation": "h", "y": 1.12, "x": 0}
        )
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
