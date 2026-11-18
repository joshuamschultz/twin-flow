"""T-225: moves.parquet, states.parquet and snapshot JSON (COMP-147, REQ-190, REQ-191)."""

from __future__ import annotations

import pathlib

import polars as pl
import pytest

from twinflow.state.recorder import (
    MOVES_FILE,
    STATES_FILE,
    StateRecorder,
    load_snapshot,
    read_moves,
    read_states,
    save_snapshot,
)
from twinflow.state.records import Ledger, Move, RecordError, Snapshot


def _snap(period: int, grey: float, setup: str | None = "GRY-S") -> Snapshot:
    return Snapshot(
        t=24.0 * period,
        period=period,
        stock={"GRY-S": grey, "BLK-S": 10.0},
        setup={"press": setup},
        queue={"press": 0},
        labor={"crew": 1},
        extra={"shift": "day", "temp": 20.5},
    )


def _moves() -> list[Move]:
    states = [_snap(0, 100.0), _snap(1, 40.0, None), _snap(2, 400.0), _snap(3, 340.0)]
    return [
        Move(
            index=i,
            before=states[i],
            action="GRY-S" if i != 1 else None,
            actor="press",
            kind="period",
            events={"demand.GRY-S": 60.0, "made.GRY-S": 0.0 if i != 1 else 420.0},
            after=states[i + 1],
            ledger=Ledger(revenue=720.0, production=10.0 * i, holding=1.25),
        )
        for i in range(3)
    ]


def test_flush_writes_both_files(tmp_path: pathlib.Path) -> None:
    recorder = StateRecorder(run_id="run-1")
    for move in _moves():
        recorder.add(move, episode=0, policy="lowest_cover")
    recorder.flush(tmp_path)

    assert (tmp_path / MOVES_FILE).is_file()
    assert (tmp_path / STATES_FILE).is_file()
    table = pl.read_parquet(tmp_path / MOVES_FILE)
    assert table.height == 3
    assert {"run_id", "episode", "policy", "before.stock.GRY-S", "ledger.total"} <= set(
        table.columns
    )
    assert table["run_id"].to_list() == ["run-1"] * 3


def test_read_moves_returns_equal_moves(tmp_path: pathlib.Path) -> None:
    recorder = StateRecorder()
    for move in _moves():
        recorder.add(move)
    recorder.flush(tmp_path)
    assert read_moves(tmp_path) == _moves()


def test_moves_keep_their_episode_order(tmp_path: pathlib.Path) -> None:
    recorder = StateRecorder()
    moves = _moves()
    for episode in (0, 1):
        for move in moves:
            recorder.add(move, episode=episode)
    recorder.flush(tmp_path)
    assert read_moves(tmp_path) == moves + moves
    episodes = pl.read_parquet(tmp_path / MOVES_FILE)["episode"].to_list()
    assert episodes == [0, 0, 0, 1, 1, 1]


def test_states_are_unique_by_digest(tmp_path: pathlib.Path) -> None:
    recorder = StateRecorder()
    moves = _moves()
    for move in [*moves, *moves]:
        recorder.add(move)
    recorder.flush(tmp_path)

    states = read_states(tmp_path)
    expected = [_snap(0, 100.0), _snap(1, 40.0, None), _snap(2, 400.0), _snap(3, 340.0)]
    assert states == expected
    digests = pl.read_parquet(tmp_path / STATES_FILE)["digest"].to_list()
    assert digests == [s.digest for s in expected]


def test_flush_with_no_moves_writes_empty_tables(tmp_path: pathlib.Path) -> None:
    StateRecorder().flush(tmp_path)
    assert read_moves(tmp_path) == []
    assert read_states(tmp_path) == []


def test_snapshot_json_round_trip(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "start.json"
    snap = _snap(4, 123.5, None)
    save_snapshot(path, snap)
    assert load_snapshot(path) == snap
    assert load_snapshot(path).digest == snap.digest


def test_load_snapshot_names_the_file_on_bad_json(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    with pytest.raises(RecordError, match="broken.json"):
        load_snapshot(path)


def test_read_moves_names_a_missing_file(tmp_path: pathlib.Path) -> None:
    with pytest.raises(FileNotFoundError, match=MOVES_FILE):
        read_moves(tmp_path)
