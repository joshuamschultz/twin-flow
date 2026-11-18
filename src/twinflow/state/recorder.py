"""COMP-147: write and read a run's moves and states (REQ-190, REQ-191).

`moves.parquet` holds one flattened row per move (`run_id`, `episode`, the `flatten` columns,
`policy`); `states.parquet` holds each distinct snapshot once, keyed by its digest, in the
order first seen. Both use polars with zstd compression (D-140: no pandas, no pyarrow).
A snapshot can also be saved as JSON and used as the start state of a new run (REQ-191).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import polars as pl

from twinflow.state.records import (
    Move,
    RecordError,
    Snapshot,
    flatten,
    flatten_snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
    unflatten,
    unflatten_snapshot,
)

MOVES_FILE = "moves.parquet"
STATES_FILE = "states.parquet"
STATE_PREFIX = "state"

_MOVES_EMPTY = {"run_id": pl.String, "episode": pl.Int64, "policy": pl.String}
_STATES_EMPTY = {"digest": pl.String}


def _frame(rows: list[dict[str, Any]], empty: dict[str, Any]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=empty)
    return pl.from_dicts(rows, infer_schema_length=None)


class StateRecorder:
    """Collects moves for one run and writes them next to the run's other logs."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id
        self._rows: list[dict[str, Any]] = []
        self._states: dict[str, Snapshot] = {}

    def add(self, move: Move, episode: int = 0, policy: str | None = None) -> None:
        self._rows.append(
            {"run_id": self.run_id, "episode": episode, **flatten(move), "policy": policy}
        )
        for snapshot in (move.before, move.after):
            self._states.setdefault(snapshot.digest, snapshot)

    def __len__(self) -> int:
        return len(self._rows)

    def flush(self, run_dir: pathlib.Path | str) -> None:
        """Write `moves.parquet` and `states.parquet` under `run_dir` (created if needed)."""
        folder = pathlib.Path(run_dir)
        folder.mkdir(parents=True, exist_ok=True)
        states = [
            {"digest": digest, **flatten_snapshot(snapshot, STATE_PREFIX)}
            for digest, snapshot in self._states.items()
        ]
        _frame(self._rows, _MOVES_EMPTY).write_parquet(folder / MOVES_FILE, compression="zstd")
        _frame(states, _STATES_EMPTY).write_parquet(folder / STATES_FILE, compression="zstd")


def _read(run_dir: pathlib.Path | str, name: str) -> list[dict[str, Any]]:
    path = pathlib.Path(run_dir) / name
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist; run with state capture to write {name}")
    return pl.read_parquet(path).to_dicts()


def read_moves(run_dir: pathlib.Path | str) -> list[Move]:
    """The moves of a run, in the order they were added."""
    return [unflatten(row) for row in _read(run_dir, MOVES_FILE)]


def read_states(run_dir: pathlib.Path | str) -> list[Snapshot]:
    """The distinct snapshots of a run, in the order first seen."""
    return [unflatten_snapshot(row, STATE_PREFIX) for row in _read(run_dir, STATES_FILE)]


def save_snapshot(path: pathlib.Path | str, snapshot: Snapshot) -> None:
    """Write a snapshot as readable JSON (REQ-191)."""
    text = json.dumps(snapshot_to_dict(snapshot), indent=2, sort_keys=True, allow_nan=False)
    pathlib.Path(path).write_text(text + "\n", encoding="utf-8")


def load_snapshot(path: pathlib.Path | str) -> Snapshot:
    """Read a snapshot saved by `save_snapshot`; a bad file raises `RecordError` naming it."""
    file = pathlib.Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError(f"{file}: cannot read the snapshot ({exc})") from exc
    if not isinstance(data, dict):
        raise RecordError(f"{file}: a snapshot file must hold one JSON object")
    try:
        return snapshot_from_dict(data)
    except RecordError as exc:
        raise RecordError(f"{file}: {exc}") from exc
