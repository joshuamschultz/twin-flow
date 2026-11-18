"""S1 (TF-006 B-28): one `MarkovView` builder, shared by the race and the states view.

A chain's states get ids (`s0`, `s1`, ...), plain words ("Grey high, natural low"; the one
source of state words, `state_words`), money per
day by ledger part, how long the plant stays, and the likely moves between them.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
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


DEFAULT_LEVELS = ("out", "low", "ok", "high")
NORMAL_LEVELS = frozenset({"ok", "normal"})


def cap(text: str) -> str:
    """First letter upper case, the rest kept (unlike str.capitalize, which lowers "OK")."""
    return text[:1].upper() + text[1:]


def _level_word(name: str) -> str:
    return "OK" if name == "ok" else name


def _join_and(items: list[str]) -> str:
    return items[0] if len(items) < 2 else f"{', '.join(items[:-1])} and {items[-1]}"


def _is_normal(level: str, names: Sequence[str]) -> bool:
    """The level a reader expects most days. It is left out of a state's title."""
    if any(n in NORMAL_LEVELS for n in names):
        return level in NORMAL_LEVELS
    odd = len(names) >= 3 and len(names) % 2 == 1
    return odd and level in names and names.index(level) == (len(names) - 1) // 2


def state_words(text: str, levels: Sequence[str] | None, line: str) -> dict[str, str]:
    """Plain words for a state label (DESIGN.md 3.8, R18). The one source of state words.

    `grey:high black:ok natural:low | black` reads "Grey high, natural low" and
    "Press on black". Families at one level are joined ("black and natural low"), the
    highest level comes first, the normal level is left out, and a state where every
    family sits at one level reads "All stock OK". A label of another shape is kept."""
    parts = label_parts(text)
    if parts is None:
        return {"title": text, "setup": ""}
    names = list(levels) if levels else list(DEFAULT_LEVELS)
    setup = parts["setup"]
    if setup is None:
        where = ""
    elif setup == "idle":
        where = f"{cap(line)} rests"
    else:
        where = f"{cap(line)} on {setup}"
    by_level: dict[str, list[str]] = {}
    for family, level in parts["levels"].items():
        by_level.setdefault(level, []).append(family)
    if len(by_level) == 1:
        return {"title": f"All stock {_level_word(next(iter(by_level)))}", "setup": where}
    rank = {name: i for i, name in enumerate(names)}
    shown = sorted(
        (lv for lv in by_level if not _is_normal(lv, names)), key=lambda lv: -rank.get(lv, -1)
    )
    title = ", ".join(f"{_join_and(by_level[lv])} {_level_word(lv)}" for lv in shown)
    return {"title": cap(title), "setup": where}


def markov_view(
    chain: MarkovChain,
    episodes: int,
    leak_states: Collection[str] = (),
    line: str = "line",
    levels: Sequence[str] | None = None,
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
                "words": state_words(state, levels, line),
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
    leaks: Sequence[Leak],
    chain: MarkovChain | None,
    line: str = "line",
    levels: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Money leaks as JSON rows with state ids and one thing to try (R19)."""
    ids = {state: f"s{i}" for i, state in enumerate(chain.states)} if chain is not None else {}
    return [
        {
            "state": leak.state,
            "state_id": ids.get(leak.state),
            "words": state_words(leak.state, levels, line),
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


__all__ = [
    "leak_rows",
    "label_parts",
    "markov_view",
    "round_dollars",
    "round_share",
    "cap",
    "state_words",
]
