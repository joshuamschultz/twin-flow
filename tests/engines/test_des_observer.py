"""T-245: periodic snapshots from the discrete-event run (COMP-152, REQ-197, D-136).

`DesStateObserver(every_hours=N)` takes a snapshot at 0, N, 2N, ... hours and one last
snapshot at the end of the run, and writes one `period` move per interval. It adds no
SimPy events, so sampling ends exactly where the run ends (drain, natural end or the
sim-time limit) and the termination reason and logs never change.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from twinflow.engines.des import DesStateObserver
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import load_plan
from twinflow.state.recorder import read_moves, read_states
from twinflow.state.records import Move

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
LOGS = ("events.parquet", "inventory.parquet", "resource_usage.parquet")
STEP_HOURS = 0.05  # 180 s: cnc-shop runs about an hour of sim time


def _run(
    example: str, tmp_path: Path, observer: DesStateObserver | None, **limits: float
) -> RunResult:
    folder = EXAMPLES / example
    model = load_model(str(folder / "model.yaml"))
    plan = load_plan(str(folder / "plan.csv"), model.registry)
    return RunDriver(model).run(
        plan,
        seed=0,
        replication_index=0,
        artifact_dir=tmp_path,
        observers=() if observer is None else (observer,),
        **limits,  # type: ignore[arg-type]
    )


def _moves(result: RunResult) -> list[Move]:
    assert result.artifact_dir is not None
    return read_moves(result.artifact_dir)


def _times(moves: list[Move]) -> list[float]:
    return [moves[0].before.t, *(move.after.t for move in moves)]


def test_samples_every_n_hours_and_once_at_the_end(tmp_path: Path) -> None:
    result = _run("cnc-shop", tmp_path, DesStateObserver(every_hours=STEP_HOURS))
    moves = _moves(result)
    times = _times(moves)
    step = STEP_HOURS * 3600
    boundaries = [k * step for k in range(int(result.horizon // step) + 1)]
    assert times[: len(boundaries)] == pytest.approx(boundaries)
    assert times[-1] == pytest.approx(result.horizon)
    assert len(times) == len(boundaries) + (0 if result.horizon % step == 0 else 1)
    assert all(move.kind == "period" for move in moves)
    assert [move.index for move in moves] == list(range(len(moves)))
    assert [move.before for move in moves[1:]] == [move.after for move in moves[:-1]]
    assert [move.before.period for move in moves] == list(range(len(moves)))


def test_snapshot_reads_the_floor(tmp_path: Path) -> None:
    result = _run("cnc-shop", tmp_path, DesStateObserver(every_hours=STEP_HOURS))
    moves = _moves(result)
    start, end = moves[0].before, moves[-1].after
    assert set(start.setup) == {
        f"{step}_{part}_m"
        for step in ("saw", "mill", "deburr", "inspect")
        for part in ("shaft", "bracket")
    }
    # The t=0 sample follows the t=0 events: the first order already holds an operator.
    assert start.labor == {"floor_pool": 5}
    assert end.labor == {"floor_pool": 6}
    assert end.wip == 0 and end.open_orders == 0
    assert end.setup["mill_shaft_m"] == "grp_mill_shaft"
    # Somewhere mid-run the mill queue backs up (the cnc-shop bottleneck).
    assert max(move.after.queue["mill_shaft"] for move in moves) > 0
    assert max(move.after.wip for move in moves) > 0
    assert result.artifact_dir is not None
    assert len(read_states(result.artifact_dir)) <= len(moves) + 1


def test_snapshot_stock_equals_the_inventory_log_at_each_sample(tmp_path: Path) -> None:
    result = _run("tier0-foundry", tmp_path, DesStateObserver(every_hours=0.02))
    assert result.artifact_dir is not None
    inventory = pl.read_parquet(result.artifact_dir / "inventory.parquet")
    snapshots = [_moves(result)[0].before, *(move.after for move in _moves(result))]
    checked = 0
    for snapshot in snapshots:
        for stock, level in snapshot.stock.items():
            rows = inventory.filter((pl.col("stock") == stock) & (pl.col("t") <= snapshot.t))
            assert level == pytest.approx(rows.sort("t", maintain_order=True)["level"][-1])
            checked += 1
    assert checked > 10


@pytest.mark.parametrize(
    ("example", "limits"),
    [
        ("cnc-shop", {}),
        ("cnc-shop", {"max_sim_time": 1000.0}),
        ("active-control", {}),  # wip-capped release with breakdowns: ends on drain
        ("tier0-foundry", {}),
    ],
)
def test_termination_and_logs_unchanged_with_an_observer(
    example: str, limits: dict[str, float], tmp_path: Path
) -> None:
    plain = _run(example, tmp_path / "plain", None, **limits)
    watched = _run(example, tmp_path / "watched", DesStateObserver(every_hours=0.05), **limits)
    assert plain.artifact_dir is not None and watched.artifact_dir is not None
    assert watched.termination_reason == plain.termination_reason
    assert (watched.event_count, watched.horizon) == (plain.event_count, plain.horizon)
    for name in LOGS:
        assert (watched.artifact_dir / name).read_bytes() == (
            plain.artifact_dir / name
        ).read_bytes()
    times = _times(_moves(watched))
    assert times[-1] == pytest.approx(watched.horizon)  # sampling ends where the run ends
    assert max(times) <= watched.horizon + 1e-9


def test_sampler_stops_at_max_sim_time(tmp_path: Path) -> None:
    result = _run("cnc-shop", tmp_path, DesStateObserver(every_hours=0.05), max_sim_time=1000.0)
    assert result.termination_reason == "sim_time_limit"
    assert _times(_moves(result))[-1] == pytest.approx(1000.0)


def test_sampler_stops_on_drain(tmp_path: Path) -> None:
    result = _run("active-control", tmp_path, DesStateObserver(every_hours=0.05))
    assert result.termination_reason == "demand_completed"
    moves = _moves(result)
    assert _times(moves)[-1] == pytest.approx(result.horizon)
    assert moves[-1].after.open_orders == 0


@pytest.mark.parametrize("hours", [0, -1.0, float("nan"), float("inf")])
def test_every_hours_must_be_a_positive_number(hours: float) -> None:
    with pytest.raises(ValueError, match="every_hours"):
        DesStateObserver(every_hours=hours)


# --- T-246: decision moves ------------------------------------------------------------------


class _AlternatingPolicy:
    """Picks EDD at even decisions and SPT at odd ones, so actions vary."""

    def __init__(self) -> None:
        self.calls = 0

    def decide(self, observation, allowed_actions):  # type: ignore[no-untyped-def]
        del observation
        self.calls += 1
        wanted = "edd" if self.calls % 2 else "spt"
        return next(action for action in allowed_actions if action.policy == wanted)


def _decision_run(tmp_path: Path, observer: DesStateObserver | None) -> tuple[RunResult, object]:
    from twinflow.policy import PolicyRuntime

    folder = EXAMPLES / "cnc-shop"
    model = load_model(str(folder / "model.yaml"))
    plan = load_plan(str(folder / "plan.csv"), model.registry)
    runtime = PolicyRuntime(_AlternatingPolicy(), "test:alternating")
    result = RunDriver(model).run(
        plan,
        seed=0,
        replication_index=0,
        artifact_dir=tmp_path,
        decision_runtime=runtime,
        observers=() if observer is None else (observer,),
    )
    return result, runtime


def test_one_decision_move_per_dispatch_decision(tmp_path: Path) -> None:
    result, runtime = _decision_run(tmp_path, DesStateObserver(every_hours=None, decisions=True))
    trace = runtime.trace  # type: ignore[attr-defined]
    moves = _moves(result)
    assert len(trace) > 5
    assert [m.kind for m in moves] == ["decision"] * len(trace)
    assert [(m.actor, m.action) for m in moves] == [
        (record.location_id, record.action.policy) for record in trace
    ]
    assert [m.before.t for m in moves] == pytest.approx([r.simulation_time for r in trace])
    # Each decision move runs to the next decision; the last runs to the end of the run.
    assert [m.after for m in moves[:-1]] == [m.before for m in moves[1:]]
    assert moves[-1].after.t == pytest.approx(result.horizon)
    # The decision state shows the queue the rule was chosen over.
    assert all(m.before.queue[m.actor] > 0 for m in moves if m.actor is not None)


def test_decisions_and_periods_together(tmp_path: Path) -> None:
    result, runtime = _decision_run(tmp_path, DesStateObserver(every_hours=0.05, decisions=True))
    moves = _moves(result)
    decisions = [m for m in moves if m.kind == "decision"]
    periods = [m for m in moves if m.kind == "period"]
    assert len(decisions) == len(runtime.trace)  # type: ignore[attr-defined]
    assert periods and periods[-1].after.t == pytest.approx(result.horizon)
    assert [m.index for m in moves] == list(range(len(moves)))


def test_decision_capture_leaves_logs_and_trace_unchanged(tmp_path: Path) -> None:
    plain, plain_runtime = _decision_run(tmp_path / "plain", None)
    watched, watched_runtime = _decision_run(
        tmp_path / "watched", DesStateObserver(every_hours=0.05, decisions=True)
    )
    assert plain.artifact_dir is not None and watched.artifact_dir is not None
    for name in (*LOGS, "decision_trace.json"):
        assert (watched.artifact_dir / name).read_bytes() == (
            plain.artifact_dir / name
        ).read_bytes()
    assert watched_runtime.trace == plain_runtime.trace  # type: ignore[attr-defined]


def test_without_decisions_flag_no_decision_moves(tmp_path: Path) -> None:
    result, _ = _decision_run(tmp_path, DesStateObserver(every_hours=0.05))
    assert {m.kind for m in _moves(result)} == {"period"}


def test_observer_needs_periods_or_decisions() -> None:
    with pytest.raises(ValueError, match="every_hours or decisions"):
        DesStateObserver(every_hours=None)
