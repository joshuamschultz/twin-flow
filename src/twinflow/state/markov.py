"""COMP-148: a Markov chain estimated from any moves table under a grouped view (REQ-199).

Ports `factory_mdp.analysis.estimate_markov` / `transition_matrix` from the RL repo: states
are the view's labels of each move's `before` snapshot; the top-K most visited become the
chain; a matrix row is divided by ALL moves out of that state, so a row sums below 1 when
some moves leave the top-K. Pure Python and numpy; no networkx or pandas (D-140), and
Mermaid draws the graph. `money` gives the mean ledger per state for money leaks (REQ-208).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

import numpy as np
import numpy.typing as npt

from twinflow.state.groups import GroupedView
from twinflow.state.records import LEDGER_PARTS, Move, Snapshot

STICKY_SELF_LOOP = 0.5  # a state that stays put at least half the time is "sticky"


@dataclass(frozen=True)
class StateMoney:
    """Mean dollars per move out of one state, in total and by cause."""

    visits: int
    total: float
    revenue: float
    production: float
    setup: float
    holding: float
    lost_sales: float
    late: float


@dataclass(frozen=True)
class Edge:
    """A likely move between two chain states."""

    source: str
    target: str
    prob: float


@dataclass(frozen=True, eq=False)
class MarkovChain:
    """The estimated chain. `states` are the top-K labels, most visited first."""

    view: str
    moves: int
    states: tuple[str, ...]
    share: Mapping[str, float]  # every visited state, most visited first
    matrix: npt.NDArray[np.float64]  # K x K: P(next = j | now = i), over ALL moves out of i
    edges: tuple[Edge, ...]
    sticky: tuple[str, ...]
    money: Mapping[str, StateMoney] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        """Share of all moves that start in a top-K state."""
        return float(sum(self.share[s] for s in self.states))

    def to_json(self) -> dict[str, Any]:
        """A plain JSON-ready dict (markov.json)."""
        sticky = set(self.sticky)
        return {
            "view": self.view,
            "moves": self.moves,
            "coverage": self.coverage,
            "states": [
                {"id": f"s{i}", "label": s, "share": self.share[s], "sticky": s in sticky}
                for i, s in enumerate(self.states)
            ],
            "matrix": self.matrix.tolist(),
            "edges": [{"from": e.source, "to": e.target, "prob": e.prob} for e in self.edges],
            "share": dict(self.share),
            "money": {s: vars(m) for s, m in self.money.items()},
        }

    def to_mermaid(self) -> str:
        """A Mermaid `flowchart LR`: one node per top state (label and share), one arrow
        per edge (odds), sticky states drawn with a thick border."""
        ids = {state: f"s{i}" for i, state in enumerate(self.states)}
        lines = ["flowchart LR"]
        lines += [
            f'    {ids[s]}["{mermaid_escape(s)}<br/>{self.share[s]:.0%}"]' for s in self.states
        ]
        lines += [f"    {ids[e.source]} -->|{e.prob:.2f}| {ids[e.target]}" for e in self.edges]
        if self.sticky:
            lines.append("    classDef sticky stroke-width:3px")
            lines.append(f"    class {','.join(ids[s] for s in self.sticky)} sticky")
        return "\n".join(lines) + "\n"


def mermaid_escape(text: str) -> str:
    """Make text safe inside a quoted Mermaid label (entity codes, `#` first)."""
    return (
        text.replace("#", "#35;").replace('"', "#quot;").replace("<", "#lt;").replace(">", "#gt;")
    )


def _labels(
    moves: Sequence[Move],
    view: GroupedView,
    demand_rate: Mapping[str, float],
    families: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    """Labels of every move's before and after snapshot; each snapshot object is labelled
    once (an engine's `after` is usually the next move's `before`)."""
    cache: dict[int, str] = {}

    def label(snapshot: Snapshot) -> str:
        key = id(snapshot)
        found = cache.get(key)
        if found is None:
            found = cache[key] = view.label(snapshot, demand_rate, families)
        return found

    return [label(m.before) for m in moves], [label(m.after) for m in moves]


def _money(moves: Sequence[Move], now: list[str]) -> dict[str, StateMoney]:
    sums: dict[str, np.ndarray] = {}
    visits: Counter[str] = Counter(now)
    for move, state in zip(moves, now, strict=True):
        ledger = move.ledger
        parts = [getattr(ledger, p) for p in LEDGER_PARTS]
        row = sums.get(state)
        if row is None:
            sums[state] = np.array(parts, dtype=float)
        else:
            row += parts
    out: dict[str, StateMoney] = {}
    for state, total in sums.items():
        mean = total / visits[state]
        means = dict(zip(LEDGER_PARTS, (float(x) for x in mean), strict=True))
        net = means["revenue"] - sum(v for k, v in means.items() if k != "revenue")
        out[state] = StateMoney(visits=visits[state], total=net, **means)
    return out


def estimate(
    moves: Sequence[Move],
    view: GroupedView,
    *,
    demand_rate: Mapping[str, float] | None = None,
    families: Mapping[str, str] | None = None,
    top_k: int = 15,
    min_prob: float = 0.05,
) -> MarkovChain:
    """Estimate the chain of `moves` under `view` (REQ-199).

    `demand_rate` and `families` are the static model facts the view's labels need (days
    of cover, family of each thing); both may be omitted for views that use neither.
    """
    if top_k < 1:
        raise ValueError(f"top_k must be at least 1, got {top_k}")
    if not 0.0 <= min_prob <= 1.0:
        raise ValueError(f"min_prob must be between 0 and 1, got {min_prob}")
    now, nxt = _labels(moves, view, demand_rate or {}, families or {})
    counts = Counter(now)
    total = len(now)
    # most visited first; ties keep first-seen order (Counter preserves insertion order)
    ranked = sorted(counts, key=lambda s: -counts[s])
    share = {s: counts[s] / total for s in ranked}
    states = tuple(ranked[:top_k])

    position = {s: k for k, s in enumerate(states)}
    pair_counts: Counter[tuple[int, int]] = Counter()
    for a, b in zip(now, nxt, strict=True):
        i = position.get(a)
        if i is not None:
            j = position.get(b)
            if j is not None:
                pair_counts[(i, j)] += 1
    matrix = np.zeros((len(states), len(states)))
    for (i, j), n in pair_counts.items():
        matrix[i, j] = n
    out_totals = np.array([counts[s] for s in states], dtype=float)
    matrix /= np.maximum(out_totals, 1.0)[:, None]

    edges = tuple(
        Edge(states[i], states[j], float(matrix[i, j]))
        for i in range(len(states))
        for j in range(len(states))
        if matrix[i, j] > 0 and matrix[i, j] >= min_prob
    )
    sticky = tuple(s for k, s in enumerate(states) if matrix[k, k] >= STICKY_SELF_LOOP)
    return MarkovChain(
        view=view.name,
        moves=total,
        states=states,
        share=MappingProxyType(share),
        matrix=matrix,
        edges=edges,
        sticky=sticky,
        money=MappingProxyType(_money(moves, now)),
    )
