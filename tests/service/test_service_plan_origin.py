"""The service's engine jobs read calendar-dated plans like the CLI does (TF-006 fix).

A shop file with a work calendar lets a plan give `start_date`/`due_date` as plain
dates, counted from the calendar `origin` (`load_plan(..., origin=plan_origin(model))`).
The CLI passed the origin; the service's run, sweep and optimize jobs did not, so a
dated plan failed there with "could not convert string to float".
"""

from __future__ import annotations

from pathlib import Path

from twinflow.service.app import _build_space, _optimize_work, _run_work, _sweep_work
from twinflow.service.schemas import LeverDomainSpec, OptimizeRequest, SweepRequest

_MODEL = """
defaults: {cycle_time_cv: 0.0}
stocks: []
part_types:
  - {name: raw, uom: piece, attributes: {}}
  - {name: done, uom: piece, attributes: {}}
machines: [{name: m}]
labor:
  pools:
    - name: p
      skills: [s]
      headcount: 1
      calendar:
        timezone: America/Chicago
        origin: "2026-11-16T06:00:00-06:00"
        weekly:
          - {days: [mon, tue, wed, thu, fri], start: "06:00", end: "22:00"}
processes: []
bom: []
locations:
  - {name: cut, consumes: [{thing: raw, qty: 1, uom: piece}],
     emits: [{thing: done, qty: 1, uom: piece}], setup_key: g,
     time_model: {kind: rate_based, rate: 0.01}, machine: m, labor_skill: s}
routing:
  - {part: done, steps: [cut]}
"""

_PLAN = (
    "work_order_id,part,qty,start_date,due_date\n"
    "wo-1,done,5,2026-11-16,2026-11-17\n"
    "wo-2,done,5,2026-11-17,2026-11-18\n"
)


def _files(tmp_path: Path) -> tuple[str, str]:
    model = tmp_path / "shop.yaml"
    model.write_text(_MODEL, encoding="utf-8")
    plan = tmp_path / "plan.csv"
    plan.write_text(_PLAN, encoding="utf-8")
    return str(model), str(plan)


def test_run_job_reads_a_calendar_dated_plan(tmp_path: Path) -> None:
    model, plan = _files(tmp_path)
    result = _run_work(model, plan, 1)
    assert result["kpis"]["on_time_pct"] == 100.0


def test_sweep_job_reads_a_calendar_dated_plan(tmp_path: Path) -> None:
    model, plan = _files(tmp_path)
    request = SweepRequest(model="shop", sweep={"locations[cut].capacity": [1, 2]}, reps=1)
    result = _sweep_work(model, plan, request)
    assert [point["kpis"]["on_time_pct"] for point in result["points"]] == [100.0, 100.0]


def test_optimize_job_reads_a_calendar_dated_plan(tmp_path: Path) -> None:
    model, plan = _files(tmp_path)
    request = OptimizeRequest(
        model="shop",
        space={"locations[cut].capacity": LeverDomainSpec(min=1, max=2, step=1)},
        budget=2,
        reps=1,
    )
    result = _optimize_work(model, plan, _build_space(request), request)
    assert result
