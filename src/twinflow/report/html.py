"""Shared pieces of the old Plotly report page, kept only for the race report.

The digital-twin run report moved to the run viewer (TF-006 B-34): `twinflow run` writes
`report.html` through `twinflow.report.run_view.run_view_model` and the offline decision
template. `twinflow.extensions.report` (the race report) still builds a Plotly and Mermaid
page and imports the four helpers below. B-33 moves the race report onto the decision
views; then this module, `_assets/mermaid.min.js` and the Plotly dependency go too.

Fully offline (D-026): Plotly and Mermaid are inlined once, UNMODIFIED, in delimited
`<script>` blocks.
"""

from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go  # type: ignore[import-untyped]  # no stub; D-026 lib choice
import plotly.offline as pyo  # type: ignore[import-untyped]  # no stub package; D-026 lib choice

_MERMAID_JS = Path(__file__).parent / "_assets" / "mermaid.min.js"


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

REPORT_STYLE = _STYLE
"""The old report's brand stylesheet, for the race report page."""


def plotly_script() -> str:
    """The Plotly library inlined once, unmodified, in its delimited block (D-026)."""
    return f"<script id='twinflow-plotly-lib'>{pyo.get_plotlyjs()}</script>"


def mermaid_script() -> str:
    """The vendored Mermaid bundle and its theme, with startOnLoad off (D-026)."""
    js = _MERMAID_JS.read_text(encoding="utf-8")
    init = (
        "mermaid.initialize({startOnLoad:false,theme:'base',"
        "themeVariables:{fontFamily:'JetBrains Mono, monospace',fontSize:'13px',"
        "primaryColor:'#EEF4FF',primaryBorderColor:'#3A6BA8',primaryTextColor:'#0B1220',"
        "lineColor:'#5A6B85'}});"
    )
    return f"<script id='twinflow-mermaid-lib'>{js}</script><script>{init}</script>"


def figure_html(fig: go.Figure, div_id: str | None = None) -> str:
    """One Plotly figure as a responsive, library-free HTML fragment. A fixed `div_id`
    makes the page the same every time it is written."""
    return fig.to_html(  # type: ignore[no-any-return]
        full_html=False,
        include_plotlyjs=False,
        default_width="100%",
        config={"responsive": True},
        div_id=div_id,
    )


__all__ = ["REPORT_STYLE", "figure_html", "mermaid_script", "plotly_script"]
