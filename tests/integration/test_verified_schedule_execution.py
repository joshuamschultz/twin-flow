"""Verified per-piece assignments are actually executed by the DES runner."""

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from twinflow.application.linear_draft import LinearDraft, compile_draft
from twinflow.application.linear_schedule import (
    firings_from_plan,
    translate_schedule,
    work_orders_from_snapshot,
)
from twinflow.plan.driver import RunDriver
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling import problem_from_dict, solve


def _fact(value, unit=None):
    return {"status": "confirmed", "value": value, "unit": unit, "source": "test fixture"}


def test_runner_replays_verified_machine_assignments(tmp_path: Path) -> None:
    draft = LinearDraft.model_validate(
        {
            "product": _fact("finished"),
            "input_part": _fact("blank"),
            "as_of": _fact("2026-10-08T00:00:00Z"),
            "operators": _fact(3, "person"),
            "simple_flow_confirmed": _fact(True),
            "stages": [
                {
                    "id": "cut",
                    "seconds_per_piece": _fact(4.0, "second/piece"),
                    "machine_capacity": _fact(2, "machine"),
                },
                {
                    "id": "pack",
                    "seconds_per_piece": _fact(2.0, "second/piece"),
                    "machine_capacity": _fact(1, "machine"),
                },
            ],
            "orders": [
                {
                    "id": "original",
                    "quantity": _fact(1, "piece"),
                    "release_second": _fact(0.0, "second"),
                    "due_second": _fact(100.0, "second"),
                }
            ],
        }
    )
    compiled = compile_draft(draft, "draft", 1)
    assert compiled["status"] == "compiled", compiled["issues"]
    capsule = ScenarioCapsule.from_dict(compiled["capsule_preview"])
    scenario = {
        "id": "scenario",
        "digest": capsule.digest,
        "draft_link": {"compiler_version": "linear-piece-1"},
        "capsule": capsule.to_dict(),
    }
    snapshot = {
        "scenario_id": "scenario",
        "scenario_digest": capsule.digest,
        "orders": [
            {
                "order_id": "new",
                "product": "finished",
                "quantity": 2,
                "release_second": 0.0,
                "due_second": 100.0,
            },
        ],
    }
    translated = translate_schedule(scenario, snapshot)
    assert translated["status"] == "translated", translated["issues"]
    solved = solve(problem_from_dict(translated["problem"]))
    assert solved.status == "FEASIBLE" and solved.verified
    firings = firings_from_plan(translated, asdict(solved))
    with pytest.raises(ValueError, match="exactly one firing"):
        RunDriver(capsule.compile().model).run(
            work_orders_from_snapshot(snapshot),
            seed=42,
            replication_index=0,
            artifact_dir=tmp_path,
            execution_schedule=firings[:-1],
        )
    simultaneous_cut = [firing for firing in firings if firing.location_id == "cut"]
    if simultaneous_cut[0].planned_start == simultaneous_cut[1].planned_start:
        overlap = [
            replace(firing, machine_id=simultaneous_cut[0].machine_id)
            if firing.operation_id == simultaneous_cut[1].operation_id
            else firing
            for firing in firings
        ]
        with pytest.raises(ValueError, match="overlapping"):
            RunDriver(capsule.compile().model).run(
                work_orders_from_snapshot(snapshot),
                seed=42,
                replication_index=0,
                artifact_dir=tmp_path,
                execution_schedule=overlap,
            )
    run = RunDriver(capsule.compile().model).run(
        work_orders_from_snapshot(snapshot),
        seed=42,
        replication_index=0,
        artifact_dir=tmp_path,
        execution_schedule=firings,
    )
    assert run.outcome == "completed"
    assert len(run.schedule_trace) == len(firings)
    by_id = {row["operation_id"]: row for row in run.schedule_trace}
    for firing in firings:
        row = by_id[firing.operation_id]
        assert row["machine_id"] == firing.machine_id
        assert row["actual_start"] >= firing.planned_start
        assert row["actual_start"] == firing.planned_start
    assert run.run_meta["execution_mode"] == "verified_schedule"
    repeated = RunDriver(capsule.compile().model).run(
        work_orders_from_snapshot(snapshot),
        seed=42,
        replication_index=0,
        artifact_dir=tmp_path,
        execution_schedule=firings,
    )
    assert repeated.schedule_trace == run.schedule_trace
