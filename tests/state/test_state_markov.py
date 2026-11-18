"""T-226: Markov chains over a grouped view (COMP-148, REQ-199, REQ-208, D-140)."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator

import numpy as np
import pytest

from twinflow.state.groups import GroupedView, Level, StockGroups, default_views
from twinflow.state.markov import ChainCounter, MarkovChain, estimate
from twinflow.state.records import Ledger, Move, Snapshot

VIEW = GroupedView(
    "bands",
    stock=StockGroups(by="units", levels=(Level("lo", 10.0), Level("mid", 20.0), Level("hi"))),
)
UNITS = {"lo": 5.0, "mid": 15.0, "hi": 25.0}

# (before, after, ledger) — shares: lo 3/7, mid 3/7, hi 1/7
STEPS = [
    ("lo", "lo", Ledger(revenue=10.0)),
    ("lo", "mid", Ledger(revenue=20.0)),
    ("lo", "lo", Ledger(holding=3.0)),
    ("mid", "mid", Ledger(revenue=5.0)),
    ("mid", "hi", Ledger(revenue=5.0, setup=2.0)),
    ("mid", "mid", Ledger(revenue=5.0)),
    ("hi", "lo", Ledger(lost_sales=7.0)),
]


def _snap(band: str, period: int = 0) -> Snapshot:
    return Snapshot(t=float(period), period=period, stock={"A": UNITS[band]})


def _moves() -> list[Move]:
    return [
        Move(i, _snap(a, i), "A", "line", "period", {}, _snap(b, i + 1), ledger)
        for i, (a, b, ledger) in enumerate(STEPS)
    ]


def _chain(**kwargs: object) -> MarkovChain:
    return estimate(_moves(), VIEW, **kwargs)  # type: ignore[arg-type]


LO, MID, HI = "A:lo", "A:mid", "A:hi"


def test_shares_cover_every_state_most_visited_first() -> None:
    chain = _chain()
    assert chain.states == (LO, MID, HI)
    assert chain.share == pytest.approx({LO: 3 / 7, MID: 3 / 7, HI: 1 / 7})
    assert chain.moves == 7
    assert chain.coverage == pytest.approx(1.0)


def test_matrix_equals_hand_values() -> None:
    expected = np.array([[2 / 3, 1 / 3, 0.0], [0.0, 2 / 3, 1 / 3], [1.0, 0.0, 0.0]])
    np.testing.assert_allclose(_chain().matrix, expected)


def test_rows_divide_by_all_moves_out_so_they_can_sum_below_one() -> None:
    chain = _chain(top_k=2)
    assert chain.states == (LO, MID)
    np.testing.assert_allclose(chain.matrix, [[2 / 3, 1 / 3], [0.0, 2 / 3]])
    assert chain.matrix[1].sum() == pytest.approx(2 / 3)
    assert chain.coverage == pytest.approx(6 / 7)


def test_sticky_states_have_a_self_loop_of_at_least_half() -> None:
    assert _chain().sticky == (LO, MID)


def test_min_prob_drops_weak_edges() -> None:
    edges = {(e.source, e.target): e.prob for e in _chain(min_prob=0.4).edges}
    assert edges == pytest.approx({(LO, LO): 2 / 3, (MID, MID): 2 / 3, (HI, LO): 1.0})
    every = {(e.source, e.target) for e in _chain(min_prob=0.0).edges}
    assert (LO, MID) in every and (MID, HI) in every
    assert (HI, MID) not in every  # never seen: no edge even at min_prob 0


def test_money_per_state_is_the_mean_ledger_of_moves_out() -> None:
    money = _chain().money
    assert money[LO].visits == 3
    assert money[LO].total == pytest.approx(9.0)
    assert money[LO].revenue == pytest.approx(10.0)
    assert money[LO].holding == pytest.approx(1.0)
    assert money[MID].setup == pytest.approx(2 / 3)
    assert money[HI].total == pytest.approx(-7.0)
    assert money[HI].lost_sales == pytest.approx(7.0)


def test_money_covers_states_outside_the_top_k() -> None:
    assert set(_chain(top_k=1).money) == {LO, MID, HI}


def test_bad_arguments_are_refused() -> None:
    with pytest.raises(ValueError, match="top_k"):
        _chain(top_k=0)
    with pytest.raises(ValueError, match="min_prob"):
        _chain(min_prob=1.5)


def test_no_moves_gives_an_empty_chain() -> None:
    chain = estimate([], VIEW)
    assert chain.states == ()
    assert chain.matrix.shape == (0, 0)
    assert chain.coverage == 0.0


def test_family_view_uses_demand_rates_and_families() -> None:
    snap = Snapshot(t=0.0, period=0, stock={"A": 1.0, "B": 50.0}, setup={"line": "A"})
    move = Move(0, snap, "A", "line", "period", {}, snap, Ledger())
    chain = estimate(
        [move],
        default_views()["family"],
        demand_rate={"A": 1.0, "B": 1.0},
        families={"A": "red", "B": "blue"},
    )
    assert chain.states == ("red:low blue:high | red",)


def test_to_json_is_plain_json() -> None:
    data = json.loads(json.dumps(_chain(top_k=2).to_json()))
    assert data["view"] == "bands"
    assert [s["label"] for s in data["states"]] == [LO, MID]
    assert data["states"][0]["sticky"] is True
    np.testing.assert_allclose(data["matrix"], [[2 / 3, 1 / 3], [0.0, 2 / 3]])
    assert data["coverage"] == pytest.approx(6 / 7)
    assert set(data["money"]) == {LO, MID, HI}


_NODE = re.compile(r'^    s\d+\["[^"]*"\]$')
_EDGE = re.compile(r"^    s\d+ -->\|\d\.\d\d\| s\d+$")
_CLASS = re.compile(r"^    (classDef sticky .+|class s\d+(,s\d+)* sticky)$")


def test_to_mermaid_is_a_valid_flowchart() -> None:
    text = _chain().to_mermaid()
    lines = text.splitlines()
    assert lines[0] == "flowchart LR"
    for line in lines[1:]:
        assert _NODE.match(line) or _EDGE.match(line) or _CLASS.match(line), line
    assert '    s0["A:lo<br/>43%"]' in lines
    assert "    s0 -->|0.67| s0" in lines
    assert "    s0 -->|0.33| s1" in lines
    assert "    class s0,s1 sticky" in lines


def test_to_mermaid_escapes_labels() -> None:
    view = GroupedView("q", stock=StockGroups(by="units", levels=(Level("x"),)))
    snap = Snapshot(t=0.0, period=0, stock={'say "hi" <b>#1': 1.0})
    chain = estimate([Move(0, snap, None, None, "event", {}, snap, Ledger())], view)
    node = chain.to_mermaid().splitlines()[1]
    assert _NODE.match(node), node
    assert "#quot;" in node and "#lt;" in node and "#gt;" in node and "#35;1" in node


def _stream(count: int, batch: int) -> Iterator[list[Move]]:
    """`count` moves in batches, each built only when it is counted (memory stays small)."""
    rng = np.random.default_rng(0)
    levels = rng.integers(0, 40, size=count + 1)

    def snap(i: int) -> Snapshot:
        v = int(levels[i])
        return Snapshot(t=float(i), period=i, stock={"A": float(v), "B": float(v % 7)},
                        setup={"l": "A"})  # fmt: skip

    after = snap(0)
    for first in range(0, count, batch):
        moves = []
        for i in range(first, min(count, first + batch)):
            before, after = after, snap(i + 1)
            moves.append(Move(i, before, "A", "l", "period", {}, after, Ledger(revenue=1.0)))
        yield moves


def test_a_chain_of_100k_moves_counts_in_under_two_seconds() -> None:
    """100,000 distinct snapshots are labelled and counted, one batch at a time
    (`ChainCounter`, the race's streaming path); `estimate` gives the same chain."""
    view = GroupedView(
        "two",
        stock=StockGroups(by="units", levels=(Level("lo", 10.0), Level("mid", 30.0), Level("hi"))),
        setup="thing",
    )
    start = time.perf_counter()
    counter = ChainCounter(view)
    for batch in _stream(100_000, 2_000):
        counter.add(batch)
    chain = counter.chain()
    elapsed = time.perf_counter() - start
    assert chain.moves == 100_000
    assert elapsed < 2.0, elapsed
    small = [m for batch in _stream(3_000, 3_000) for m in batch]
    counted = ChainCounter(view)
    for batch in _stream(3_000, 500):
        counted.add(batch)
    once, streamed = estimate(small, view), counted.chain()
    assert once.states == streamed.states and once.share == streamed.share
    assert np.array_equal(once.matrix, streamed.matrix)
    assert dict(once.money) == dict(streamed.money)


def test_a_plain_function_can_be_the_view() -> None:
    """T-259: a callable `Snapshot -> label` drives the chain like a grouped view."""

    def bands(snapshot: Snapshot) -> str:
        units = snapshot.stock["A"]
        return "A:lo" if units < 10 else "A:mid" if units < 20 else "A:hi"

    chain = estimate(_moves(), bands)
    expected = _chain()
    assert chain.states == expected.states and chain.view == "bands"
    assert np.array_equal(chain.matrix, expected.matrix)
    assert chain.money == expected.money


def test_a_named_callable_view_keeps_its_name() -> None:
    class Named:
        name = "my_view"

        def __call__(self, snapshot: Snapshot) -> str:
            return "all"

    assert estimate(_moves(), Named()).view == "my_view"
