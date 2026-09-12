from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan
from twinflow.policy import (
    DispatchAction,
    MaskedActionError,
    PolicyObservation,
    PolicyRuntime,
    ReplayPolicy,
    StaleStateError,
)

_MODEL = """
defaults: {cycle_time_cv: 0.0}
machines: [{name: m1}]
part_types:
  - {name: raw, uom: piece, attributes: {}}
  - {name: widget, uom: piece, attributes: {}}
stocks: []
labor: {pools: [{name: p, headcount: 1, skills: [op]}]}
locations:
  - name: work
    consumes: [{thing: raw, qty: 1, uom: piece}]
    emits: [{thing: widget, qty: 1, uom: piece}]
    setup_key: g
    dispatch: fifo
    time_model: {kind: rate_based, rate: 1.0}
    machine: m1
    labor_skill: op
routing: [{part: widget, steps: [work]}]
"""
_PLAN = "work_order_id,part,qty,start_date,due_date\nA,widget,2,0,10000\nB,widget,8,0,5\n"


class EarliestDueDatePolicy:
    def decide(
        self, observation: PolicyObservation, allowed_actions: tuple[DispatchAction, ...]
    ) -> DispatchAction:
        del observation
        return next(action for action in allowed_actions if action.policy == "edd")


def _inputs(tmp_path: Path) -> tuple[object, list[object]]:
    (tmp_path / "model.yaml").write_text(_MODEL)
    (tmp_path / "plan.csv").write_text(_PLAN)
    model = load_model(str(tmp_path / "model.yaml"))
    return model, load_plan(str(tmp_path / "plan.csv"), model.registry)  # type: ignore[return-value]


def test_policy_changes_real_forward_dispatch_and_writes_trace(tmp_path: Path) -> None:
    model, plan = _inputs(tmp_path)
    runtime = PolicyRuntime(EarliestDueDatePolicy(), "policies/edd-v1.py")
    result = RunDriver(model).run(  # type: ignore[arg-type]
        plan, seed=7, replication_index=0, artifact_dir=tmp_path / "runs", decision_runtime=runtime
    )
    events = pl.read_parquet(result.event_log_path).sort("actual_start")
    persisted = json.loads((result.artifact_dir / "decision_trace.json").read_text())  # type: ignore[operator]

    assert float(events["qty"][0]) == 8.0
    assert runtime.trace[0].action.policy == "edd"
    assert persisted[0]["observation_digest"].startswith("sha256:")
    assert persisted[0]["policy_artifact"] == "policies/edd-v1.py"


def test_seeded_trace_replay_reproduces_events(tmp_path: Path) -> None:
    model, plan = _inputs(tmp_path)
    original = PolicyRuntime(EarliestDueDatePolicy(), "policies/edd-v1.py")
    first = RunDriver(model).run(  # type: ignore[arg-type]
        plan,
        seed=11,
        replication_index=0,
        artifact_dir=tmp_path / "first",
        decision_runtime=original,
    )
    replay = PolicyRuntime(ReplayPolicy(original.trace), "trace:first", fallback_on_error=False)
    second = RunDriver(model).run(  # type: ignore[arg-type]
        plan,
        seed=11,
        replication_index=0,
        artifact_dir=tmp_path / "second",
        decision_runtime=replay,
    )
    first_events = pl.read_parquet(first.event_log_path).to_dicts()
    second_events = pl.read_parquet(second.event_log_path).to_dicts()
    assert first_events == second_events
    original_actions = [record.action for record in original.trace]
    assert [record.action for record in replay.trace] == original_actions


def test_runtime_rejects_stale_and_masked_actions() -> None:
    runtime = PolicyRuntime(EarliestDueDatePolicy(), "policy")
    runtime.choose("work", 0.0, "fifo", ())
    with pytest.raises(StaleStateError):
        runtime.act(DispatchAction("work", "fifo"), expected_state_version=0)
    with pytest.raises(MaskedActionError):
        runtime.act(DispatchAction("other", "fifo"), expected_state_version=1)
