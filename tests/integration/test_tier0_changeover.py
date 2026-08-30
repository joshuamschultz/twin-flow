"""Tier 0 — changeover time: a declared `changeover_seconds` is charged as setup.

The dispatch loop locks a warm machine to its established setup group (D-047), so
the reachable changeover in a single-group location is the cold-start setup of the
first firing. That setup time must (a) elapse in simulation and (b) surface in the
`setup_hours` KPI, which was hardcoded to 0.0 before Tier 0.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_CHANGEOVER = 90.0


def _model_yaml(changeover_seconds: float) -> str:
    return textwrap.dedent(
        f"""
        stocks:
          - {{name: raw, uom: piece}}
        part_types:
          - {{name: raw, uom: piece, attributes: {{}}}}
          - {{name: widget, uom: piece, attributes: {{}}}}
        machines:
          - {{name: press_m}}
        labor:
          pools:
            - {{name: pool, headcount: 1, skills: [op]}}
        locations:
          - name: press
            consumes: [{{thing: raw, qty: 1, uom: piece}}]
            emits: [{{thing: widget, qty: 1, uom: piece}}]
            setup_key: grp_widget
            changeover_seconds: {changeover_seconds}
            time_model: {{kind: rate_based, rate: 1.0}}
            machine: press_m
            labor_skill: op
        routing:
          - {{part: widget, steps: [press]}}
        processes: []
        bom: []
        """
    ).strip()


def _plan_csv() -> str:
    return "work_order_id,part,qty,start_date,due_date\nwo-1,widget,3,0,10000\n"


def _run(tmp_path: Path, changeover_seconds: float) -> tuple[float, float]:
    model_path = tmp_path / "model.yaml"
    model_path.write_text(_model_yaml(changeover_seconds), encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    plan_path.write_text(_plan_csv(), encoding="utf-8")

    compiled = load_model(str(model_path))
    plan = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(plan, seed=0, replication_index=0)
    orders = orders_frame(plan, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    return kpis.setup_hours, result.horizon


def test_changeover_seconds_surface_as_setup_hours(tmp_path) -> None:
    setup_hours, _ = _run(tmp_path, _CHANGEOVER)
    # One cold-start changeover is charged (subsequent same-group firings pay 0).
    assert setup_hours == _CHANGEOVER / 3600.0


def test_zero_changeover_keeps_setup_hours_zero(tmp_path) -> None:
    setup_hours, _ = _run(tmp_path, 0.0)
    assert setup_hours == 0.0


def test_changeover_time_actually_elapses_in_the_run(tmp_path) -> None:
    _, horizon_with = _run(tmp_path, _CHANGEOVER)
    _, horizon_without = _run(tmp_path, 0.0)
    # The declared setup time is spent in simulation, so the run finishes later.
    assert horizon_with - horizon_without == _CHANGEOVER
