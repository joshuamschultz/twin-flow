"""T-244: the run observer seam on `RunDriver.run` (COMP-152, REQ-197, D-136).

No observer, no change: the event and inventory logs are byte-identical to a run without
the new argument. An observer that only reads changes nothing either. `start` and
`finalize` are each called exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan
from twinflow.plan.observers import RunObserver, RunView

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
LOGS = ("events.parquet", "inventory.parquet", "resource_usage.parquet")


class Recorder:
    """Records every call; reads the view at each call to prove reads change nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.view: RunView | None = None
        self.advances: list[float] = []
        self.final: tuple[Path, str] | None = None

    def start(self, view: RunView) -> None:
        self.calls.append("start")
        self.view = view
        view.stock_levels()

    def on_advance(self, next_time: float) -> None:
        assert self.view is not None
        self.advances.append(next_time)
        self.view.queue_lengths()
        self.view.setups()
        self.view.orders()

    def on_decision(self, location_id: str, time: float, rule: str) -> None:
        self.calls.append("decision")

    def finalize(self, run_dir: Path, termination_reason: str) -> None:
        self.calls.append("finalize")
        self.final = (run_dir, termination_reason)


def _inputs(name: str):
    folder = EXAMPLES / name
    model = load_model(str(folder / "model.yaml"))
    return model, load_plan(str(folder / "plan.csv"), model.registry)


def _logs(run_dir: Path) -> dict[str, bytes]:
    return {name: (run_dir / name).read_bytes() for name in LOGS}


@pytest.mark.parametrize("example", ["cnc-shop", "tier0-foundry"])
def test_no_observer_and_a_reading_observer_leave_logs_byte_identical(
    example: str, tmp_path: Path
) -> None:
    model, plan = _inputs(example)
    plain = RunDriver(model).run(plan, seed=3, replication_index=0, artifact_dir=tmp_path / "a")
    empty = RunDriver(model).run(
        plan, seed=3, replication_index=0, artifact_dir=tmp_path / "b", observers=()
    )
    recorder = Recorder()
    watched = RunDriver(model).run(
        plan, seed=3, replication_index=0, artifact_dir=tmp_path / "c", observers=[recorder]
    )
    assert plain.artifact_dir is not None and empty.artifact_dir is not None
    assert watched.artifact_dir is not None
    reference = _logs(plain.artifact_dir)
    assert _logs(empty.artifact_dir) == reference
    assert _logs(watched.artifact_dir) == reference
    assert (watched.termination_reason, watched.event_count, watched.horizon) == (
        plain.termination_reason,
        plain.event_count,
        plain.horizon,
    )
    # One `on_advance` per SimPy step, each before the step it announces.
    assert len(recorder.advances) == watched.event_count
    assert recorder.advances == sorted(recorder.advances)


def test_start_and_finalize_are_called_once_with_the_run_dir(tmp_path: Path) -> None:
    model, plan = _inputs("cnc-shop")
    first, second = Recorder(), Recorder()
    result = RunDriver(model).run(
        plan, seed=0, replication_index=0, artifact_dir=tmp_path, observers=(first, second)
    )
    for recorder in (first, second):
        assert recorder.calls.count("start") == 1
        assert recorder.calls[0] == "start"
        assert recorder.calls[-1] == "finalize"
        assert recorder.calls.count("finalize") == 1
        assert recorder.final == (result.artifact_dir, result.termination_reason)
    # The logs are already on disk when finalize runs.
    assert result.artifact_dir is not None
    assert (result.artifact_dir / "events.parquet").is_file()


def test_view_shows_the_live_floor(tmp_path: Path) -> None:
    model, plan = _inputs("cnc-shop")
    recorder = Recorder()
    RunDriver(model).run(
        plan, seed=0, replication_index=0, artifact_dir=tmp_path, observers=[recorder]
    )
    view = recorder.view
    assert view is not None
    assert view.now > 0  # the run is over; the view still reads the end state
    assert set(view.stock_levels()) == {"round_bar", "square_billet"}
    assert set(view.queue_lengths()) == set(view.in_flight())
    assert sum(view.queue_lengths().values()) == 0
    assert len(view.setups()) == 8
    assert view.setups()["mill_shaft_m"] == "grp_mill_shaft"
    assert view.labor_headcount() == {"floor_pool": 6}
    assert view.labor_available() == {"floor_pool": 6}
    orders = view.orders()
    assert len(orders) == 10
    assert all(order.completed_at is not None for order in orders)
    assert view.open_orders() == 0


def test_recorder_satisfies_the_protocol() -> None:
    assert isinstance(Recorder(), RunObserver)
