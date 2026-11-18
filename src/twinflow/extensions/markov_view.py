"""S1 (TF-006 B-28): one `MarkovView` builder, shared by the race and the states view.

A chain's states get ids (`s0`, `s1`, ...), plain words ("Grey high, natural low"), money per
day by ledger part, how long the plant stays, and the likely moves between them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from twinflow.extensions.race import Leak
from twinflow.state.markov import MarkovChain
from twinflow.state.records import LEDGER_PARTS

LEAK_TRY = {  # one thing to try, keyed on a leak's biggest cause (R19)
    "setup": "Try longer runs of one family so the {line} changes over less.",
    "holding": "Try smaller lots or lower targets so less stock sits on the shelf.",
    "lost_sales": "Try starting the short products sooner so they do not run out.",
    "late": "Try putting the late orders first.",
}


def round_dollars(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def round_share(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


_dollars = round_dollars
_share = round_share


def label_parts(text: str) -> dict[str, Any] | None:
    """`grey:ok black:high | natural` as levels and setup, when a label has that shape."""
    stock, _, setup = text.partition(" | ")
    pairs = [p.split(":", 1) for p in stock.split()]
    if not pairs or any(len(p) != 2 for p in pairs):
        return None
    return {"levels": {k: v for k, v in pairs}, "setup": setup or None}


def node_words(text: str, parts: Mapping[str, Any] | None, line: str) -> dict[str, str]:
    """`grey:high black:ok natural:low | black` as "Grey high, natural low" and
    "Press on black" (R18). Levels named "ok" are left out of the title."""
    if parts is None:
        return {"title": text, "setup": ""}
    shown = [f"{k} {v}".replace("_", " ") for k, v in parts["levels"].items() if v != "ok"]
    title = ", ".join(shown).capitalize() if shown else "All stock ok"
    setup = parts["setup"]
    if setup is None:
        where = ""
    elif setup == "idle":
        where = f"{line.capitalize()} rests"
    else:
        where = f"{line.capitalize()} on {setup}"
    return {"title": title, "setup": where}


def markov_view(
    chain: MarkovChain, episodes: int, leak_states: Collection[str] = (), line: str = "line"
) -> dict[str, Any]:
    """The chain as the `MarkovView` of DATA-CONTRACT 1 and 3 (S1): node ids, words, money by
    part, stay share and arrows. Shared by the race report and the states view."""
    ids = {state: f"s{i}" for i, state in enumerate(chain.states)}
    sticky = set(chain.sticky)
    nodes = []
    for i, state in enumerate(chain.states):
        money = chain.money.get(state)
        per_day = money.total if money is not None else 0.0
        visits = money.visits if money is not None else 0
        parts = {p: _dollars(getattr(money, p)) for p in LEDGER_PARTS} if money else {}
        leak = (
            visits * sum(getattr(money, p) for p in ("setup", "holding", "lost_sales", "late"))
            if money is not None
            else 0.0
        )
        nodes.append(
            {
                "id": ids[state],
                "label": state,
                "parts_of_label": label_parts(state),
                "words": node_words(state, label_parts(state), line),
                "share": _share(chain.share[state]),
                "visits": visits,
                "sticky": state in sticky,
                "stay": _share(float(chain.matrix[i, i])),
                "per_day": _dollars(per_day),
                "per_run": _dollars(visits * per_day / max(1, episodes)),
                "leak_per_run": _dollars(leak / max(1, episodes)),
                "money_per_day": parts,
                "losing": per_day < 0,
                "top_leak": state in leak_states,
            }
        )
    edges = [
        {
            "from": ids[e.source],
            "to": ids[e.target],
            "prob": _share(e.prob),
            "self": e.source == e.target,
        }
        for e in chain.edges
    ]
    return {
        "view": chain.view,
        "moves": chain.moves,
        "coverage": _share(chain.coverage),
        "nodes": nodes,
        "edges": edges,
    }


def leak_rows(
    leaks: Sequence[Leak], chain: MarkovChain | None, line: str = "line"
) -> list[dict[str, Any]]:
    """Money leaks as JSON rows with state ids and one thing to try (R19)."""
    ids = {state: f"s{i}" for i, state in enumerate(chain.states)} if chain is not None else {}
    return [
        {
            "state": leak.state,
            "state_id": ids.get(leak.state),
            "share": round_share(leak.share),
            "visits": leak.visits,
            "per_visit": round_dollars(leak.per_visit),
            "per_run": round_dollars(leak.per_run),
            "parts": {k: round_dollars(v) for k, v in leak.parts.items()},
            "cause": leak.cause,
            "cause_words": leak.cause_words,
            "try": LEAK_TRY.get(leak.cause, "Look at what the {line} does here.").format(line=line),
        }
        for leak in leaks
    ]


__all__ = ["leak_rows", "label_parts", "markov_view", "node_words", "round_dollars", "round_share"]
