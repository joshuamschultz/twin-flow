"""T-179 (RED): shop-floor state in execution (REQ-130, REQ-131, COMP-134, COMP-135).

In-progress, paused (TF-005 ruling B), held, hold until, done, split children (TF-003 D-084)
and material ready time, on one hand-built problem at now = 100 whose plan the verifier
accepts. Executor contract: test_fidelity.py. The problem mirrors `_plans.generate_state`
with fixed numbers.

API beyond the test_fidelity.py contract, fixed by these tests:

- `OpRecord.job_id` of a split child op `X~k` is the child job `J~k` (the compiled job).
- `OpRecord.gate_time`: when the material gate opened (`max(release, ready_time, links)`,
  release after `hold until` as `scheduling.apply_overrides` compiles it).
- Plan control takes the planned start as an earliest start only, so a hand-made plan
  whose start is before an op's gate still waits for the gate (two tests use such a plan
  to show the gate binds; it is not verified).

Shop: machines M1..M5 in W, no setups except where a test adds them; pool CREW (size 1)
works from 150, so the paused op PAU-0 (one option M2, hard machine-only lock) cannot start
at now.
"""

from __future__ import annotations

import pytest
from _handplans import (
    EXACT,
    StubSource,
    a,
    assert_replays,
    hand_plan,
    op,
    option,
    override,
    records,
    request,
    run_plan,
    verified_plan,
)

NOW = 100.0
CREW_OPENS = 150.0
CENTERS = [{"id": "W"}]
MACHINES = [{"id": f"M{k}", "work_center_id": "W"} for k in range(1, 6)]


def _chain(job_id: str, ops: list, **extra: object) -> dict:
    for pred, succ in zip(ops, ops[1:], strict=False):
        succ.setdefault("predecessors", []).append({"operation_id": pred["id"]})
    return {"id": job_id, "release": NOW, "operations": ops, **extra}


def _state_jobs() -> list:
    running = op(
        "RUN-0",
        "W",
        [option("M1", 20.0), option("M3", 20.0)],
        status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 30.0},
    )
    paused = op(
        "PAU-0",
        "W",
        [option("M2", 40.0)],
        operator_needs=[{"pool_id": "CREW", "quantity": 1, "phase": "both"}],
    )
    done_first = op("DON-0", "W", [option("M3", 10.0)], status={"state": "done"})
    material = op(
        "MAT-0",
        "W",
        [option("M5", 10.0)],
        ready_time=CREW_OPENS,
        predecessors=[{"operation_id": "SPL-1"}],
    )
    return [
        _chain("RUN", [running, op("RUN-1", "W", [option("M2", 10.0)])]),
        _chain("PAU", [paused, op("PAU-1", "W", [option("M4", 10.0)])]),
        _chain("HST", [op("HST-0", "W", [option("M1", 10.0)])], status="held"),
        _chain("HOV", [op("HOV-0", "W", [option("M2", 10.0)])]),
        _chain("HUN", [op("HUN-0", "W", [option("M3", 20.0)])]),
        _chain("DON", [done_first, op("DON-1", "W", [option("M1", 10.0)])]),
        _chain("FIN", [op("FIN-0", "W", [option("M4", 10.0)])], status="done"),
        _chain(
            "SPL",
            [
                op("SPL-0", "W", [option("M1", 4.0, 2.0), option("M2", 4.0, 2.0)], 4.0),
                op("SPL-1", "W", [option("M4", 4.0, 2.0)], 4.0),
            ],
        ),
        _chain("MAT", [material]),
        _chain("RDY", [op("RDY-0", "W", [option("M1", 10.0)], ready_time=CREW_OPENS)]),
    ]


def _state_request() -> object:
    return request(
        _state_jobs(),
        MACHINES,
        CENTERS,
        now=NOW,
        calendars=[{"id": "CREWCAL", "intervals": [(CREW_OPENS, 1000.0)]}],
        pools=[{"id": "CREW", "size": 1, "calendar_id": "CREWCAL"}],
        overrides=[
            override("paused-lock", "lock", operation_id="PAU-0", machine_id="M2"),
            override("hold-out", "hold", job_id="HOV", until=None),
            override("hold-until", "hold", job_id="HUN", until=160.0),
            override("split", "split", job_id="SPL", fractions=(0.5, 0.5)),
        ],
    )


# Split children run q 2: 4 + 2 x 2 = 8 s. MAT-0 waits for both SPL-1~k (ends 164).
STATE_PLAN = [
    a("RUN-0", "M1", 100, 130),
    a("RUN-1", "M2", 130, 140),
    a("DON-1", "M1", 130, 140),
    a("SPL-0~1", "M1", 140, 148),
    a("SPL-0~2", "M2", 140, 148),
    a("RDY-0", "M1", 150, 160),
    a("SPL-1~1", "M4", 148, 156),
    a("SPL-1~2", "M4", 156, 164),
    a("PAU-0", "M2", 150, 190),
    a("PAU-1", "M4", 190, 200),
    a("HUN-0", "M3", 160, 180),
    a("MAT-0", "M5", 164, 174),
]


def _state_plan() -> tuple:
    return verified_plan(_state_request(), STATE_PLAN)


def test_former_guard_initial_or_completed_wip_state_plan_replays_exactly() -> None:
    compiled, plan = _state_plan()

    assert_replays(run_plan(compiled, plan), plan)


# --- in progress -----------------------------------------------------------------------------


def test_in_progress_op_starts_at_now_on_its_status_machine_with_remaining_seconds() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    r = found["RUN-0"]
    assert r.machine_id == "M1"
    assert r.start == pytest.approx(NOW, abs=EXACT)
    assert r.setup_seconds == 0.0
    assert r.setup_end == pytest.approx(NOW, abs=EXACT)
    assert r.end == pytest.approx(NOW + 30.0, abs=EXACT)


def test_in_progress_remaining_seconds_take_the_run_factor() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan, source=StubSource(run={"M1": 2.0})))

    assert found["RUN-0"].start == pytest.approx(NOW, abs=EXACT)
    assert found["RUN-0"].end == pytest.approx(NOW + 60.0, abs=EXACT)


def test_in_progress_op_gets_no_setup_where_the_machine_has_a_setup_function() -> None:
    jobs = [
        _chain(
            "R",
            [
                op(
                    "R-0",
                    "W",
                    [option("M1", 20.0)],
                    status={"state": "in_progress", "machine_id": "M1", "remaining_seconds": 30.0},
                )
            ],
        ),
        _chain("N", [op("N-0", "W", [option("M1", 10.0)])]),
    ]
    req = request(
        jobs,
        [{"id": "M1", "work_center_id": "W"}],
        [{"id": "W", "setup_function_id": "S"}],
        now=NOW,
        setup_functions=[{"id": "S", "terms": [], "default_seconds": 5.0}],
    )
    compiled, plan = verified_plan(req, [a("R-0", "M1", 100, 130), a("N-0", "M1", 130, 145, 135)])

    found = records(run_plan(compiled, plan))

    assert found["R-0"].setup_seconds == 0.0
    assert found["R-0"].end == pytest.approx(130.0, abs=EXACT)
    assert found["N-0"].setup_seconds == pytest.approx(5.0, abs=EXACT)


# --- paused ----------------------------------------------------------------------------------


def test_paused_op_resumes_on_its_locked_machine_no_earlier_than_its_crew_window() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    r = found["PAU-0"]
    assert r.machine_id == "M2"
    assert r.start >= CREW_OPENS - EXACT
    assert r.start == pytest.approx(CREW_OPENS, abs=EXACT)
    assert r.end == pytest.approx(190.0, abs=EXACT)


# --- held and done ---------------------------------------------------------------------------


def test_held_jobs_and_done_work_are_absent_from_the_run() -> None:
    compiled, plan = _state_plan()

    run = run_plan(compiled, plan)

    ids = {r.operation_id for r in run.records}
    assert ids.isdisjoint({"HST-0", "HOV-0", "DON-0", "FIN-0"})
    assert ids == {x.operation_id for x in STATE_PLAN}
    assert run.termination == "completed"


def test_done_predecessor_counts_as_finished_at_now() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    # DON-1 waits only on the done DON-0 and on M1 (free at 130 after RUN-0).
    assert found["DON-1"].gate_time == pytest.approx(NOW, abs=EXACT)
    assert found["DON-1"].start == pytest.approx(130.0, abs=EXACT)


def test_hold_until_delays_release() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    assert found["HUN-0"].gate_time == pytest.approx(160.0, abs=EXACT)
    assert found["HUN-0"].start == pytest.approx(160.0, abs=EXACT)


# --- split children --------------------------------------------------------------------------


def test_split_children_run_as_separate_jobs() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    assert "SPL-0" not in found and "SPL-1" not in found
    assert found["SPL-0~1"].job_id == "SPL~1"
    assert found["SPL-0~2"].job_id == "SPL~2"
    assert found["SPL-1~2"].job_id == "SPL~2"
    assert found["SPL-1~1"].end - found["SPL-1~1"].setup_end == pytest.approx(8.0, abs=EXACT)


def test_split_children_remap_links_so_the_successor_waits_for_every_child() -> None:
    compiled, plan = _state_plan()

    # M4 at factor 2: SPL-1~1 [148, 164), SPL-1~2 [164, 180); MAT-0 waits for the later.
    found = records(run_plan(compiled, plan, source=StubSource(run={"M4": 2.0})))

    assert found["SPL-1~2"].end == pytest.approx(180.0, abs=EXACT)
    assert found["MAT-0"].gate_time == pytest.approx(180.0, abs=EXACT)
    assert found["MAT-0"].start == pytest.approx(180.0, abs=EXACT)


# --- material ready time (former guard: stocks and materials) ------------------------------


def test_former_guard_stocks_and_materials_ready_time_gates_the_op() -> None:
    compiled, plan = _state_plan()

    found = records(run_plan(compiled, plan))

    assert found["RDY-0"].gate_time == pytest.approx(CREW_OPENS, abs=EXACT)
    assert found["RDY-0"].start == pytest.approx(CREW_OPENS, abs=EXACT)


def _gates_request() -> object:
    jobs = [
        _chain("R", [op("RDY-0", "W", [option("M1", 10.0)], ready_time=150.0)]),
        _chain("H", [op("HUN-0", "W", [option("M2", 20.0)])]),
    ]
    return request(
        jobs,
        MACHINES[:2],
        CENTERS,
        now=NOW,
        overrides=[override("hold-until", "hold", job_id="H", until=160.0)],
    )


def test_ready_time_and_hold_until_gate_even_a_plan_that_starts_earlier() -> None:
    compiled, plan = hand_plan(
        _gates_request(), [a("RDY-0", "M1", 100, 110), a("HUN-0", "M2", 100, 120)]
    )

    found = records(run_plan(compiled, plan))

    assert found["RDY-0"].start == pytest.approx(150.0, abs=EXACT)
    assert found["RDY-0"].end == pytest.approx(160.0, abs=EXACT)
    assert found["HUN-0"].start == pytest.approx(160.0, abs=EXACT)
    assert found["HUN-0"].end == pytest.approx(180.0, abs=EXACT)
