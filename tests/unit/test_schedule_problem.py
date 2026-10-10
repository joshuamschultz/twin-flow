"""Capsule + schedule snapshot -> v2 SolveRequest translator (T-105, COMP-013)."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from twinflow.application.schedule_problem import (
    TRANSLATOR_VERSION,
    Translation,
    build_request,
    firings_from_plan,
)
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import WorkOrder
from twinflow.scenario import ScenarioCapsule
from twinflow.scheduling.model import (
    Assignment,
    FamilyMatrixEntry,
    OperatorNeed,
    Schedule,
    SolveResult,
)
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate

HMLV = (
    Path(__file__).resolve().parents[2] / "examples" / "hmlv-calendar" / "hmlv-calendar.twin.yaml"
)

ModelEdit = Callable[[dict[str, Any]], None]


# --- fixtures -------------------------------------------------------------------------------


def _model(steps: tuple[str, ...] = ("cut", "pack"), capacity: int = 1) -> dict[str, Any]:
    """A linear one-piece model: blank -> step outputs -> finished, one operator pool."""
    things = ["blank", *(f"{step}_out" for step in steps[:-1]), "finished"]
    locations = [
        {
            "name": step,
            "consumes": [{"thing": things[index], "qty": 1, "uom": "piece"}],
            "emits": [{"thing": things[index + 1], "qty": 1, "uom": "piece"}],
            "setup_key": f"setup_{step}",
            "time_model": {"kind": "rate_based", "rate": 0.25},
            "machine": f"machine_{step}",
            "capacity": capacity,
            "labor_skill": "operator",
        }
        for index, step in enumerate(steps)
    ]
    return {
        "defaults": {"cycle_time_cv": 0.0},
        "stocks": [],
        "part_types": [{"name": thing, "uom": "piece", "attributes": {}} for thing in things],
        "machines": [{"name": f"machine_{step}"} for step in steps],
        "labor": {
            "pools": [
                {"name": "operators", "headcount": capacity * len(steps), "skills": ["operator"]}
            ]
        },
        "locations": locations,
        "routing": [{"part": "finished", "steps": list(steps)}],
    }


def _capsule(model: dict[str, Any]) -> ScenarioCapsule:
    return ScenarioCapsule.from_dict(
        {
            "schema_version": "0.1",
            "model": model,
            "snapshot": {
                "id": "snap",
                "as_of": "2026-10-08T00:00:00+00:00",
                "model_revision": "1",
                "production_plan": [
                    {
                        "work_order_id": "seed",
                        "part": "finished",
                        "qty": 1,
                        "start_date": "0",
                        "due_date": "100",
                    }
                ],
            },
            "experiment": {"id": "exp", "replications": 1, "seed": 1},
            "assumptions": [],
            "provenance": {"source": "test"},
            "required_capabilities": [],
        }
    )


def _scenario(capsule: ScenarioCapsule) -> dict[str, Any]:
    return {"id": "scenario", "digest": capsule.digest, "capsule": capsule.to_dict()}


def _order(order_id: str, quantity: int, release: float = 0.0, due: float = 1e5) -> dict[str, Any]:
    return {
        "order_id": order_id,
        "product": "finished",
        "quantity": quantity,
        "release_second": release,
        "due_second": due,
    }


def _snapshot(scenario: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "schedule-snapshot",
        "scenario_id": scenario["id"],
        "scenario_digest": scenario["digest"],
        "orders": orders,
    }


def _translate(model: dict[str, Any], orders: list[dict[str, Any]]) -> Translation:
    scenario = _scenario(_capsule(model))
    return build_request(scenario, _snapshot(scenario, orders))


def _ready(translation: Translation) -> Any:
    assert translation.status == "ready", translation.issues
    assert translation.request is not None
    return translation.request


def _hmlv() -> tuple[dict[str, Any], dict[str, Any]]:
    scenario = _scenario(ScenarioCapsule.load(HMLV))
    return scenario, _snapshot(scenario, [_order("A", 2, due=86_400), _order("B", 1, due=86_400)])


# --- mapping --------------------------------------------------------------------------------


def test_one_operation_per_order_and_routing_step_with_order_quantity() -> None:
    request = _ready(_translate(_model(), [_order("a", 3), _order("b", 5, release=10.0)]))

    jobs = request.problem.jobs
    assert [job.id for job in jobs] == ["order-0", "order-1"]
    assert [[op.id for op in job.operations] for job in jobs] == [
        ["order-0:stage-cut", "order-0:stage-pack"],
        ["order-1:stage-cut", "order-1:stage-pack"],
    ]
    assert [op.quantity for op in jobs[1].operations] == [5.0, 5.0]
    assert jobs[1].release == 10.0 and jobs[1].due == 1e5
    pack = jobs[0].operations[1]
    assert [link.operation_id for link in pack.predecessors] == ["order-0:stage-cut"]
    option = pack.options[0]
    assert (option.fixed_seconds, option.unit_seconds) == (0.0, 4.0)


def test_machine_ids_match_the_des_naming() -> None:
    single = _ready(_translate(_model(), [_order("a", 1)]))
    double = _ready(_translate(_model(capacity=2), [_order("a", 1)]))

    assert [m.id for m in single.problem.machines] == ["machine_cut", "machine_pack"]
    assert [m.id for m in double.problem.machines] == [
        "machine_cut-1",
        "machine_cut-2",
        "machine_pack-1",
        "machine_pack-2",
    ]
    cut = double.problem.jobs[0].operations[0]
    assert [option.machine_id for option in cut.options] == ["machine_cut-1", "machine_cut-2"]
    assert {wc.id for wc in double.problem.work_centers} == {"cut", "pack"}


def test_labor_skill_becomes_operator_need_held_for_setup_and_run() -> None:
    request = _ready(_translate(_model(), [_order("a", 1)]))

    op = request.problem.jobs[0].operations[0]
    assert op.operator_needs == (OperatorNeed(pool_id="operators", quantity=1, phase="both"),)
    assert [(p.id, p.size) for p in request.problem.operator_pools] == [("operators", 2)]


def test_labor_calendar_expands_into_working_intervals() -> None:
    scenario, snapshot = _hmlv()

    request = _ready(build_request(scenario, snapshot))

    pool = request.problem.operator_pools[0]
    calendar = next(c for c in request.problem.calendars if c.id == pool.calendar_id)
    assert calendar.intervals[:2] == ((0.0, 30_600.0), (86_400.0, 117_000.0))
    weekend = [start for start, _ in calendar.intervals if 5 * 86_400 <= start < 7 * 86_400]
    assert weekend == []
    assert request.problem.jobs[0].operations[0].shift_crossing == "pause"


def test_setup_key_and_changeover_become_a_family_matrix_function() -> None:
    scenario, snapshot = _hmlv()

    request = _ready(build_request(scenario, snapshot))

    work_center = request.problem.work_centers[0]
    function = next(
        f for f in request.problem.setup_functions if f.id == work_center.setup_function_id
    )
    assert function.terms == ()
    assert function.family_matrix == (
        FamilyMatrixEntry(from_family="finished-family", to_family="finished-family", seconds=0.0),
    )
    assert function.default_seconds == 600.0
    assert request.problem.jobs[0].operations[0].setup_family == "finished-family"
    assert [m.id for m in request.problem.machines] == ["mill-1", "mill-2"]


def test_no_changeover_means_no_setup_function() -> None:
    request = _ready(_translate(_model(), [_order("a", 1)]))

    assert request.problem.setup_functions == ()
    assert all(wc.setup_function_id is None for wc in request.problem.work_centers)


def test_600_orders_by_8_steps_translate_without_caps_and_validate() -> None:
    steps = tuple(f"s{index}" for index in range(8))
    orders = [_order(f"o{index}", 7, release=float(index)) for index in range(600)]

    translation = _translate(_model(steps), orders)

    request = _ready(translation)
    assert translation.issues == ()
    assert sum(len(job.operations) for job in request.problem.jobs) == 4_800
    assert validate(request) == []


def test_translation_metadata_and_stable_digest() -> None:
    first = _translate(_model(), [_order("a", 3)])
    second = _translate(_model(), [_order("a", 3)])
    changed = _translate(_model(), [_order("a", 4)])

    assert first.translator_version == TRANSLATOR_VERSION == "capsule-ops-2"
    assert first.problem_digest == second.problem_digest
    assert first.problem_digest != changed.problem_digest
    canonical = json.dumps(
        _ready(first).model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )
    assert first.problem_digest == hashlib.sha256(canonical.encode()).hexdigest()


def test_scenario_revision_mismatch_is_blocked() -> None:
    scenario = _scenario(_capsule(_model()))
    snapshot = _snapshot(scenario, [_order("a", 1)]) | {"scenario_digest": "other"}

    translation = build_request(scenario, snapshot)

    assert translation.status == "blocked" and translation.request is None
    assert [issue.code for issue in translation.issues] == ["scenario_mismatch"]


# --- unsupported features -------------------------------------------------------------------


def _location(model: dict[str, Any], name: str = "cut") -> dict[str, Any]:
    return next(loc for loc in model["locations"] if loc["name"] == name)


def _distribution(model: dict[str, Any]) -> None:
    _location(model)["time_model"] = {"kind": "distribution", "dist": "lognormal", "mean": 4}


def _batch_hold(model: dict[str, Any]) -> None:
    _location(model)["time_model"] = {"kind": "batch_hold", "seconds": 30}


def _quality_gate(model: dict[str, Any]) -> None:
    _location(model)["quality_gate"] = {
        "branches": [{"prob": 0.9, "to": "pack"}, {"prob": 0.1, "to": None}]
    }


def _split_output(model: dict[str, Any]) -> None:
    _location(model)["split_output"] = True


def _material_stock(model: dict[str, Any]) -> None:
    model["stocks"] = [{"name": "resin", "uom": "kg", "initial": 100}]
    model["part_types"].append({"name": "resin", "uom": "kg", "attributes": {}})
    _location(model)["material"] = {"stock": "resin", "qty": 1, "uom": "kg"}


def _shared_machine(model: dict[str, Any]) -> None:
    _location(model, "pack")["machine"] = "machine_cut"


def _rework_cycle(model: dict[str, Any]) -> None:
    model["max_rework"] = 2
    pack = _location(model, "pack")
    pack["consumes"].append({"thing": "finished", "qty": 1, "uom": "piece"})
    model["routing"][0]["steps"] = ["cut", "pack", "pack"]


def _skill_in_several_pools(model: dict[str, Any]) -> None:
    model["labor"]["pools"].append({"name": "spares", "headcount": 1, "skills": ["operator"]})


def _batch_size(model: dict[str, Any]) -> None:
    _location(model)["batch_size"] = "4 piece"


def _breakdown(model: dict[str, Any]) -> None:
    _location(model)["breakdown"] = {"mtbf": "8h", "mttr": {"dist": "lognormal", "mean": "5m"}}


def _absence(model: dict[str, Any]) -> None:
    model["labor"]["pools"][0]["absence_rate"] = 0.1


def _release_policy(model: dict[str, Any]) -> None:
    model["release"] = {"policy": "wip_cap", "wip_cap": 2}


def _overtime_on_calendar(model: dict[str, Any]) -> None:
    model["labor"]["pools"][0]["calendar"] = {
        "timezone": "UTC",
        "origin": "2026-10-05T00:00:00+00:00",
        "weekly": [{"days": ["mon", "tue", "wed", "thu", "fri"], "start": "07:00", "end": "15:00"}],
    }
    _location(model, "pack")["shift_crossing"] = "pause"  # only `cut` keeps the default overtime


def _finish_unattended(model: dict[str, Any]) -> None:
    _location(model)["shift_crossing"] = "finish_unattended"


def _scrap_output(model: dict[str, Any]) -> None:
    model["part_types"].append({"name": "cut_scrap", "uom": "piece", "attributes": {}})
    cut = _location(model)
    cut["emits"] = [{"thing": "cut_out", "uom": "piece"}]
    cut["scrap"] = {"rate": 0.05, "thing": "cut_scrap", "uom": "piece"}


def _non_unit_ratio(model: dict[str, Any]) -> None:
    _location(model)["emits"][0]["qty"] = 2


UNSUPPORTED: list[tuple[str, ModelEdit]] = [
    ("distribution_time_model", _distribution),
    ("batch_hold_time_model", _batch_hold),
    ("quality_gate", _quality_gate),
    ("split_output", _split_output),
    ("material_stock", _material_stock),
    ("shared_machine", _shared_machine),
    ("rework_cycle", _rework_cycle),
    ("skill_in_several_pools", _skill_in_several_pools),
    ("batch_size", _batch_size),
    ("machine_breakdown", _breakdown),
    ("labor_absence", _absence),
    ("release_policy", _release_policy),
    ("unsupported_shift_crossing", _overtime_on_calendar),
    ("unsupported_shift_crossing", _finish_unattended),
    ("multiple_outputs", _scrap_output),
    ("non_unit_ratio", _non_unit_ratio),
]


@pytest.mark.parametrize(
    ("code", "edit"), UNSUPPORTED, ids=[f"{code}-{edit.__name__}" for code, edit in UNSUPPORTED]
)
def test_each_unsupported_feature_yields_its_own_named_issue(code: str, edit: ModelEdit) -> None:
    model = copy.deepcopy(_model())
    edit(model)

    translation = _translate(model, [_order("a", 2)])

    assert translation.status == "blocked"
    assert translation.request is None and translation.problem_digest is None
    assert [issue.code for issue in translation.issues] == [code]


def test_several_unsupported_features_each_get_an_issue() -> None:
    model = copy.deepcopy(_model())
    _split_output(model)
    _batch_size(model)

    translation = _translate(model, [_order("a", 2)])

    assert sorted(issue.code for issue in translation.issues) == ["batch_size", "split_output"]


# --- firings --------------------------------------------------------------------------------


def _verified(assignments: tuple[Assignment, ...]) -> SolveResult:
    schedule = Schedule(
        status="FEASIBLE", assignments=assignments, solver="test", elapsed_seconds=0.0
    )
    return SolveResult(schedule=schedule, verified=True)


def test_firings_expand_each_lot_into_pieces_after_setup() -> None:
    translation = _translate(_model(("cut",)), [_order("a", 3)])
    assignment = Assignment(
        operation_id="order-0:stage-cut",
        machine_id="machine_cut",
        start=10.0,
        setup_end=15.0,
        end=27.0,
    )

    firings = firings_from_plan(translation, _verified((assignment,)))

    assert [(f.operation_id, f.piece_id) for f in firings] == [
        ("order-0:piece-0:stage-cut", "order-0:piece-0"),
        ("order-0:piece-1:stage-cut", "order-0:piece-1"),
        ("order-0:piece-2:stage-cut", "order-0:piece-2"),
    ]
    assert [(f.planned_start, f.planned_end) for f in firings] == [
        (15.0, 19.0),
        (19.0, 23.0),
        (23.0, 27.0),
    ]
    assert {(f.location_id, f.machine_id) for f in firings} == {("cut", "machine_cut")}


def test_firings_require_a_verified_plan() -> None:
    translation = _translate(_model(("cut",)), [_order("a", 1)])
    unverified = _verified(()).model_copy(update={"verified": False})

    with pytest.raises(ValueError, match="verified"):
        firings_from_plan(translation, unverified)


def test_firings_require_every_operation_assigned() -> None:
    translation = _translate(_model(), [_order("a", 1)])
    only_cut = Assignment(
        operation_id="order-0:stage-cut", machine_id="machine_cut", start=0, setup_end=0, end=4
    )

    with pytest.raises(ValueError, match="every"):
        firings_from_plan(translation, _verified((only_cut,)))


def test_solved_plan_firings_pass_the_des_replay_guard() -> None:
    model = _model(capacity=2)
    capsule = _capsule(model)
    scenario = _scenario(capsule)
    orders = [_order("a", 3), _order("b", 2, release=5.0)]
    translation = build_request(scenario, _snapshot(scenario, orders))

    result = solve(_ready(translation))
    firings = firings_from_plan(translation, result)

    assert result.verified
    assert len(firings) == (3 + 2) * 2
    plan = [
        WorkOrder(
            work_order_id=order["order_id"],
            part=order["product"],
            qty=order["quantity"],
            start_date=str(order["release_second"]),
            due_date=str(order["due_second"]),
        )
        for order in orders
    ]
    run = RunDriver(capsule.compile().model).run(plan, 7, 0, execution_schedule=firings)
    assert run is not None


# --- end to end ------------------------------------------------------------------------------


def test_example_capsule_translates_and_solves_verified() -> None:
    scenario, snapshot = _hmlv()

    translation = build_request(scenario, snapshot)
    result = solve(_ready(translation))

    assert result.verified, result.issues
    assert result.schedule.status in ("FEASIBLE", "OPTIMAL")
