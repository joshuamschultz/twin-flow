"""TF-006 B-28: the data behind the states view (DATA-CONTRACT 3).

`states_view_model(run_dir, ...)` reads the moves a run captured, estimates the Markov chain
and returns one JSON-ready dict: a headline, where the money goes, the chain in the shared
`MarkovView` shape (`markov_view`), the money leaks and the notes the page shows.

The reading code lives here so `twinflow states`, `twinflow markov` and the page share it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from twinflow import __version__
from twinflow.extensions.markov_view import leak_rows, markov_view, round_dollars
from twinflow.extensions.race import money_leaks
from twinflow.state.groups import GroupedView, default_views
from twinflow.state.markov import MarkovChain, estimate
from twinflow.state.recorder import MOVES_FILE, read_moves
from twinflow.state.records import LEDGER_PARTS, Move

SCHEMA_VERSION = 1
SCENARIO_FILE = "scenario.json"
CAPTURE_HINT = (
    "Turn on state capture: experiment.state_capture: {every_hours: 4} for a factory model, "
    "then run again. A make-to-stock run writes moves by itself."
)
NO_FACTS = (
    "Note: no product facts (families, demand rates) were found for this run, "
    "so view {view} cannot be built. Showing view full instead."
)
GRID_NOTE = "Add `state_groups` to your model to get a clearer map."
PART_WORDS = {
    "revenue": "sales",
    "production": "making product",
    "setup": "changeovers",
    "holding": "keeping stock",
    "lost_sales": "lost sales",
    "late": "late orders",
}
RARE_SHARE = 0.05  # a state needs this share of days to be named in the headline


class StatesError(Exception):
    """A problem the user can fix. The message says where and how."""


class NoMovesError(StatesError):
    """The folder holds no captured moves."""


@dataclass(frozen=True)
class Gathered:
    """Every move found under one folder."""

    folder: Path
    runs: int
    episodes: int
    moves: tuple[Move, ...]


@dataclass(frozen=True)
class Facts:
    """The static model facts a grouped view needs, and the views the model defines."""

    demand_rate: Mapping[str, float] | None
    families: Mapping[str, str] | None
    views: Mapping[str, GroupedView]
    line: str | None = None


# --- finding and reading the moves -------------------------------------------------------------


def gather(run_dir: str | Path) -> Gathered:
    """Read every `moves.parquet` at or under `run_dir`."""
    folder = Path(run_dir)
    if not folder.is_dir():
        raise StatesError(
            f"{folder} does not exist or is not a folder. "
            "Fix: give the folder a run wrote, such as the one after --out."
        )
    files = sorted(folder.rglob(MOVES_FILE))
    moves: list[Move] = []
    episodes = 0
    for file in files:
        moves.extend(read_moves(file.parent))
        episodes += int(pl.read_parquet(file, columns=["episode"])["episode"].n_unique())
    if not moves:
        raise NoMovesError(f"No moves found in {folder}. {CAPTURE_HINT}")
    return Gathered(folder, len(files), episodes, tuple(moves))


def facts_near(folder: Path) -> Facts:
    """Model facts from the `scenario.json` a run wrote beside its replication folders."""
    views = default_views()
    for place in (folder, *folder.parents[:2]):
        found = place / SCENARIO_FILE
        if found.is_file():
            return _facts_from(found, views)
    return Facts(None, None, views)


def _facts_from(file: Path, views: dict[str, GroupedView]) -> Facts:
    from twinflow.engines.step.model import parse_step_model

    try:
        model = json.loads(file.read_text(encoding="utf-8")).get("model", {})
    except (OSError, ValueError, AttributeError):
        return Facts(None, None, views)
    if model.get("domain") != "make_to_stock":
        return Facts(None, None, views)
    parsed, _ = parse_step_model(model)
    if parsed is None:
        return Facts(None, None, views)
    return Facts(
        {p.id: p.demand_rate for p in parsed.products},
        {p.id: p.family for p in parsed.products},
        {**views, **parsed.views},
        parsed.line_id.replace("_", " "),
    )


def choose_view(name: str, facts: Facts) -> tuple[GroupedView, str | None]:
    """The view to use, and a note when it had to fall back to `full`."""
    if name not in facts.views:
        known = ", ".join(sorted(facts.views))
        raise StatesError(f"No view named {name!r}. Fix: use one of: {known}.")
    if name != "full" and facts.demand_rate is None:
        return facts.views["full"], NO_FACTS.format(view=name)
    return facts.views[name], None


def estimate_chain(
    gathered: Gathered, view_name: str, top: int, min_prob: float
) -> tuple[MarkovChain, str | None]:
    """The chain for a view, and the note when the view fell back to `full`."""
    facts = facts_near(gathered.folder)
    view, note = choose_view(view_name, facts)
    chain = estimate(
        gathered.moves,
        view,
        demand_rate=facts.demand_rate,
        families=facts.families,
        top_k=top,
        min_prob=min_prob,
    )
    return chain, note


# --- the view model ----------------------------------------------------------------------------


def _levels(view: GroupedView) -> list[dict[str, Any]] | None:
    if view.stock is None:
        return None
    return [{"name": x.name, "below": x.below} for x in view.stock.levels]


def _money_text(value: int | None) -> str:
    amount = 0 if value is None else value
    return f"{'-' if amount < 0 else ''}${abs(amount):,}"


def _headline(nodes: list[dict[str, Any]], average: int) -> str:
    """The costliest common situation in plain words (DESIGN 5)."""
    common = [n for n in nodes if (n["share"] or 0) >= RARE_SHARE]
    worst = min(common, key=lambda n: n["per_day"] or 0, default=None)
    if worst is None or (worst["per_day"] or 0) >= average:
        top = nodes[0] if nodes else None
        if top is None:
            return "No situations were found."
        return (
            f"The plant spends {round(100 * (top['share'] or 0))}% of its days in "
            f"\u201c{top['words']['title']}\u201d. A normal day makes {_money_text(average)}."
        )
    return (
        f"The plant spends {round(100 * (worst['share'] or 0))}% of its days in "
        f"\u201c{worst['words']['title']}\u201d. Those days make {_money_text(worst['per_day'])}. "
        f"A normal day makes {_money_text(average)}."
    )


def _summary(model: Mapping[str, Any]) -> list[str]:
    about, nodes = model["about"], model["chain"]["nodes"]
    lines = [model["headline"]]
    if nodes:
        lines.append(
            f"The map shows the {len(nodes)} most common situations. "
            f"They cover {round(100 * (model['chain']['coverage'] or 0))}% of all days."
        )
        lines.append(
            f"The data holds {about['moves']} moves from {about['runs']} run folders "
            f"({about['episodes']} test runs), seen through the {about['view_name']} view."
        )
    if model["leaks"]:
        top = model["leaks"][0]
        lines.append(
            f'The costliest leak is "{_leak_title(model, top)}": '
            f"{_money_text(top['per_run'])} per run, mostly {top['cause_words']}."
        )
    if about.get("note"):
        lines.append(str(about["note"]))
    return lines


def _leak_title(model: Mapping[str, Any], leak: Mapping[str, Any]) -> str:
    for node in model["chain"]["nodes"]:
        if node["id"] == leak["state_id"]:
            return str(node["words"]["title"])
    return str(leak["state"])


def _empty(folder: Path, view: str, top: int, min_prob: float) -> dict[str, Any]:
    """The page for a run with no moves: no chain, and the fix in plain words."""
    said = "No moves were captured for this run."
    return {
        "view": "states",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": {
            "title": f"States: {folder.name}",
            "project": folder.name,
            "runs": 0,
            "episodes": 0,
            "moves": 0,
            "time_span": None,
            "states_seen": 0,
            "view_name": view,
            "views_available": [],
            "top": top,
            "min_prob": min_prob,
            "note": said,
        },
        "headline": said,
        "avg_per_period": 0,
        "money": {part: 0 for part in LEDGER_PARTS},
        "chain": {"view": view, "moves": 0, "coverage": 0.0, "nodes": [], "edges": []},
        "levels": None,
        "families": None,
        "leaks": [],
        "agent_summary": [said, CAPTURE_HINT],
        "warnings": [],
        "problems": [{"message": said, "fix": CAPTURE_HINT}],
    }


def states_view_model(
    run_dir: str | Path, view: str = "family", top: int = 15, min_prob: float = 0.05
) -> dict[str, Any]:
    """The states view model for a run folder (DATA-CONTRACT 3, S1 to S4)."""
    folder = Path(run_dir)
    try:
        gathered = gather(folder)
    except NoMovesError:
        return _empty(folder, view, top, min_prob)
    chain, note = estimate_chain(gathered, view, top, min_prob)
    facts = facts_near(gathered.folder)
    chosen, _ = choose_view(view, facts)
    moves, episodes = gathered.moves, max(1, gathered.episodes)
    line = facts.line or "line"
    has_families = facts.families is not None and chosen.stock is not None
    leaks = money_leaks(chain, gathered.episodes)
    nodes_chain = markov_view(chain, gathered.episodes, {x.state for x in leaks[:1]}, line)
    if not has_families:  # exact labels are not "levels | setup": show them as they are
        for node in nodes_chain["nodes"]:
            node["parts_of_label"] = None
            node["words"] = {"title": node["label"], "setup": ""}
    average = round_dollars(sum(m.ledger.total for m in moves) / len(moves)) or 0
    families = (
        list(dict.fromkeys(facts.families.values())) if has_families and facts.families else None
    )
    about = {
        "title": f"States: {gathered.folder.name}",
        "project": gathered.folder.name,
        "runs": gathered.runs,
        "episodes": gathered.episodes,
        "moves": len(moves),
        "time_span": {
            "from": min(m.before.t for m in moves),
            "to": max(m.after.t for m in moves),
            "unit": "days" if facts.families is not None else "seconds",
        },
        "states_seen": len({s.digest for m in moves for s in (m.before, m.after)}),
        "view_name": chain.view,
        "views_available": sorted(facts.views),
        "top": top,
        "min_prob": min_prob,
        "note": " ".join(x for x in (note, None if has_families else GRID_NOTE) if x) or None,
    }
    model: dict[str, Any] = {
        "view": "states",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": about,
        "headline": _headline(nodes_chain["nodes"], average),
        "avg_per_period": average,
        "money": {
            part: round_dollars(sum(getattr(m.ledger, part) for m in moves) / episodes)
            for part in LEDGER_PARTS
        },
        "chain": nodes_chain,
        "levels": _levels(chosen) if has_families else None,
        "families": families,
        "leaks": leak_rows(leaks, chain, line),
        "warnings": [],
        "problems": [],
    }
    model["agent_summary"] = _summary(model)
    return model


__all__ = ["NoMovesError", "StatesError", "gather", "states_view_model"]
