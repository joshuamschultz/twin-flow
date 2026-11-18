"""B-03: write a decision view (race, run, states, improve, project) as one offline HTML file.

The page is the React view built into `assets/report.template.html` (web/, `npm run
build:report`). Python only fills four placeholders: the view model as JSON, the title, the
description and a `<noscript>` block with the decision in plain HTML, so the file still says
something useful in a mail preview or with scripts off (DESIGN.md 11.2 and 11.4).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping, Sequence
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

DATA = "/*__TWINFLOW_DATA__*/"
TITLE = "__TWINFLOW_TITLE__"
DESCRIPTION = "__TWINFLOW_DESCRIPTION__"
NOSCRIPT = "<!--__TWINFLOW_NOSCRIPT__-->"


@cache
def template() -> str:
    """The committed offline template (no Node needed at run time)."""
    return (
        resources.files("twinflow.report")
        .joinpath("assets/report.template.html")
        .read_text(encoding="utf-8")
    )


def _json(view_model: Mapping[str, Any]) -> str:
    """JSON that is safe inside a `<script>` tag: no `</`, no U+2028 or U+2029."""
    text = json.dumps(view_model, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _text(value: Any) -> str:
    """One line of model text for an attribute or element, escaped."""
    plain = " ".join(str(value).split()).replace("\u2028", " ").replace("\u2029", " ")
    return html.escape(plain, quote=True)


def _summary(view_model: Mapping[str, Any]) -> list[str]:
    lines = view_model.get("agent_summary")
    if isinstance(lines, Sequence) and not isinstance(lines, str) and lines:
        return [str(x) for x in lines]
    for key in ("recommendation", "answer", "outcome", "status"):
        part = view_model.get(key)
        if isinstance(part, Mapping):
            for field in ("sentence", "headline"):
                if part.get(field):
                    return [str(part[field])]
    if view_model.get("headline"):
        return [str(view_model["headline"])]
    return []


def _title(view_model: Mapping[str, Any]) -> str:
    about = view_model.get("about")
    if isinstance(about, Mapping) and about.get("title"):
        return str(about["title"])
    return f"twinflow {view_model.get('view', 'report')}"


def _rows(view_model: Mapping[str, Any]) -> str:
    """The top of the ranking as a plain table, for readers without scripts."""
    policies = view_model.get("policies")
    if not isinstance(policies, Sequence) or not policies:
        return ""
    body = []
    for row in list(policies)[:12]:
        if not isinstance(row, Mapping):
            continue
        dollars = row.get("dollars") or {}
        mean = dollars.get("mean") if isinstance(dollars, Mapping) else None
        money = "" if mean is None else f"${mean:,.0f}".replace("$-", "\u2212$")
        body.append(
            f"<tr><td>{_text(row.get('rank', ''))}</td>"
            f"<td>{_text(row.get('label') or row.get('policy', ''))}</td>"
            f"<td>{_text(money)}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Rank</th><th>Rule</th><th>Profit per run</th></tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table>"
    )


def _noscript(view_model: Mapping[str, Any]) -> str:
    parts = [f"<h1>{_text(_title(view_model))}</h1>"]
    parts += [f"<p>{_text(line)}</p>" for line in _summary(view_model)]
    cautions = view_model.get("cautions")
    if isinstance(cautions, Sequence) and cautions:
        items = "".join(
            f"<li><b>{_text(c.get('title', ''))}.</b> {_text(c.get('body', ''))}</li>"
            for c in cautions
            if isinstance(c, Mapping)
        )
        parts.append(f"<p>Before you switch:</p><ul>{items}</ul>")
    parts.append(_rows(view_model))
    parts.append("<p>Turn on scripts to see the charts.</p>")
    return "".join(parts)


def render(view_model: Mapping[str, Any]) -> str:
    """The offline HTML page for one view model (DESIGN.md 11.2)."""
    head, sep, tail = template().partition(DATA)
    if not sep:
        raise RuntimeError("The report template has no data placeholder. Rebuild it.")
    fills = {
        TITLE: _text(_title(view_model)),
        DESCRIPTION: _text(" ".join(_summary(view_model))),
        NOSCRIPT: _noscript(view_model),
    }
    # One pass, so model text is never searched again for a placeholder.
    head = re.sub("|".join(re.escape(k) for k in fills), lambda m: fills[m.group(0)], head)
    return head + _json(view_model) + tail


def write(path: str | Path, view_model: Mapping[str, Any]) -> Path:
    """Write the offline file and return its path."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(view_model), encoding="utf-8")
    return out


__all__ = ["render", "template", "write"]
