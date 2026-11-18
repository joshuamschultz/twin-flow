"""T-222: `Snapshot`, `Ledger`, `Move` and the flat row form (COMP-144, REQ-188, REQ-189)."""

from __future__ import annotations

import dataclasses
import math

import pytest

from twinflow.state.records import (
    Ledger,
    Move,
    RecordError,
    Snapshot,
    flatten,
    snapshot_from_dict,
    snapshot_to_dict,
    unflatten,
)


def _snapshot(**overrides: object) -> Snapshot:
    fields: dict[str, object] = {
        "t": 24.0,
        "period": 1,
        "stock": {"GRY-S": 120.0, "BLK-S": 45.5},
        "setup": {"press": "GRY-S", "spare": None},
        "queue": {"mill": 3, "lathe": 0},
        "wip": 4,
        "open_orders": 7,
        "labor": {"operators": 2},
        "extra": {"note": "warm", "temp": 21.5},
    }
    fields.update(overrides)
    return Snapshot(**fields)  # type: ignore[arg-type]


def _move(**overrides: object) -> Move:
    fields: dict[str, object] = {
        "index": 3,
        "before": _snapshot(),
        "action": "BLK-S",
        "actor": "press",
        "kind": "period",
        "events": {"demand.GRY-S": 60.0, "made.BLK-S": 225.0, "lost.GRY-S": 0.0},
        "after": _snapshot(t=48.0, period=2, stock={"GRY-S": 60.0, "BLK-S": 225.0}),
        "ledger": Ledger(revenue=720.0, production=1701.0, setup=200.0, holding=10.5),
    }
    fields.update(overrides)
    return Move(**fields)  # type: ignore[arg-type]


# --- frozen records ---------------------------------------------------------------------------


@pytest.mark.parametrize("record", [_snapshot(), Ledger(), _move()])
def test_records_are_frozen(record: object) -> None:
    field = dataclasses.fields(record)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(record, field, None)


def test_snapshot_mappings_are_read_only_copies() -> None:
    stock = {"A": 1.0}
    snap = _snapshot(stock=stock)
    stock["A"] = 99.0
    assert snap.stock["A"] == 1.0
    with pytest.raises(TypeError):
        snap.stock["A"] = 2.0  # type: ignore[index]


def test_snapshot_is_hashable_and_equal_by_value() -> None:
    assert _snapshot() == _snapshot()
    assert hash(_snapshot()) == hash(_snapshot())
    assert len({_snapshot(), _snapshot()}) == 1


# --- digest -----------------------------------------------------------------------------------


def test_digest_is_stable_across_mapping_order() -> None:
    one = _snapshot(stock={"A": 1.0, "B": 2.0}, setup={"x": "A", "y": None})
    two = _snapshot(stock={"B": 2.0, "A": 1.0}, setup={"y": None, "x": "A"})
    assert one.digest == two.digest
    assert len(one.digest) == 64


def test_digest_treats_int_and_float_stock_alike() -> None:
    assert _snapshot(stock={"A": 1}).digest == _snapshot(stock={"A": 1.0}).digest


@pytest.mark.parametrize(
    "change",
    [
        {"t": 24.5},
        {"period": 2},
        {"stock": {"GRY-S": 121.0, "BLK-S": 45.5}},
        {"setup": {"press": "BLK-S", "spare": None}},
        {"setup": {"press": "GRY-S", "spare": "GRY-S"}},
        {"queue": {"mill": 4, "lathe": 0}},
        {"wip": 5},
        {"open_orders": 8},
        {"labor": {"operators": 3}},
        {"extra": {"note": "cold", "temp": 21.5}},
    ],
)
def test_digest_is_sensitive_to_every_value(change: dict[str, object]) -> None:
    assert _snapshot(**change).digest != _snapshot().digest


# --- validation -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ({"t": math.nan}, "t"),
        ({"stock": {"A": math.inf}}, "stock['A']"),
        ({"extra": {"k": -math.inf}}, "extra['k']"),
    ],
)
def test_non_finite_numbers_are_rejected_by_name(change: dict[str, object], named: str) -> None:
    with pytest.raises(RecordError, match=named.replace("[", r"\[").replace("]", r"\]")):
        _snapshot(**change)


def test_non_finite_event_and_ledger_values_are_rejected() -> None:
    with pytest.raises(RecordError, match="events"):
        _move(events={"demand.A": math.nan})
    with pytest.raises(RecordError, match="holding"):
        Ledger(holding=math.inf)


def test_bad_move_kind_is_rejected() -> None:
    with pytest.raises(RecordError, match="kind"):
        _move(kind="tick")


# --- ledger -----------------------------------------------------------------------------------


def test_ledger_total_is_revenue_minus_every_cost() -> None:
    ledger = Ledger(
        revenue=100.0, production=20.0, setup=5.0, holding=3.0, lost_sales=2.0, late=1.0
    )
    assert ledger.total == pytest.approx(69.0)


def test_ledgers_add_part_by_part() -> None:
    one = Ledger(revenue=10.0, holding=1.0)
    two = Ledger(revenue=5.0, setup=2.0, late=0.5)
    assert one + two == Ledger(revenue=15.0, setup=2.0, holding=1.0, late=0.5)
    assert sum([one, two], Ledger()) == one + two


# --- flatten / unflatten ----------------------------------------------------------------------


def test_flatten_uses_the_sdd_column_names() -> None:
    row = flatten(_move())
    assert row["index"] == 3
    assert row["kind"] == "period"
    assert row["actor"] == "press"
    assert row["action"] == "BLK-S"
    assert row["before.t"] == 24.0
    assert row["before.stock.GRY-S"] == 120.0
    assert row["before.setup.press"] == "GRY-S"
    assert row["before.setup.spare"] is None
    assert row["before.queue.mill"] == 3
    assert row["before.labor.operators"] == 2
    assert row["before.extra.note"] == "warm"
    assert row["after.period"] == 2
    assert row["events.demand.GRY-S"] == 60.0
    assert row["ledger.revenue"] == 720.0
    assert row["ledger.total"] == pytest.approx(720.0 - 1701.0 - 200.0 - 10.5)


def test_flatten_unflatten_round_trip() -> None:
    move = _move()
    assert unflatten(flatten(move)) == move


def test_round_trip_with_dotted_ids_and_no_action() -> None:
    snap = _snapshot(stock={"a.b": 1.0}, setup={"line.1": None}, extra={})
    move = _move(before=snap, after=snap, action=None, actor=None, kind="event", events={})
    assert unflatten(flatten(move)) == move


def test_unflatten_ignores_recorder_columns() -> None:
    row = {**flatten(_move()), "run_id": "r1", "episode": 0, "policy": "idle"}
    assert unflatten(row) == _move()


def test_snapshot_dict_round_trip() -> None:
    snap = _snapshot()
    assert snapshot_from_dict(snapshot_to_dict(snap)) == snap


def test_snapshot_from_dict_names_a_missing_field() -> None:
    data = snapshot_to_dict(_snapshot())
    del data["wip"]
    with pytest.raises(RecordError, match="wip"):
        snapshot_from_dict(data)
