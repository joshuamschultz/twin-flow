"""B-26: the run facts the run viewer needs, saved in the run folder (DATA-CONTRACT.md 2.1).

`twinflow run` writes two files next to `kpis.json` and `intervals.json`:

- `plan.json` (G1): the plan rows, so a report can be rebuilt from the run folder alone.
- `run_detail.json`: per test run lateness (G2), time at each work centre split into work
  and wait per order (G3), mean queue wait per job per centre (G4), the routes as plain lists
  (G5), labels for centres and parts (G9), the first run's tables (the old report's detail
  tables, same numbers), shared resource rows and the assumptions.

All times here are in seconds. `run_view.run_view_model` picks the display unit (G7).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import polars as pl

from twinflow.instrumentation.kpis import KpiSet
from twinflow.model.loader import load_raw_model
from twinflow.primitives.calendar import AlwaysWorkingCalendar, WorkingCalendar
from twinflow.report.assumptions import Assumption

if TYPE_CHECKING:
    from twinflow.model import CompiledModel
    from twinflow.plan.loader import WorkOrder

DETAIL_SCHEMA_VERSION = 1
PLAN_FILE = "plan.json"
DETAIL_FILE = "run_detail.json"


def plan_rows(work_orders: Sequence[WorkOrder]) -> list[dict[str, Any]]:
    """G1: the plan as plain rows. Times stay in seconds; a blank due date is `None`."""
    return [
        {
            "id": order.work_order_id,
            "part": order.part,
            "qty": order.qty,
            "release": _seconds(order.start_date),
            "due": _seconds(order.due_date),
            "priority": order.priority,
        }
        for order in work_orders
    ]


def _seconds(text: object) -> float | None:
    try:
        return float(cast(str, text))
    except (TypeError, ValueError):
        return None


def labels(model_path: str | Path) -> dict[str, dict[str, str]]:
    """G9: the optional `label` on locations and part types. Missing labels are left out;
    the view falls back to the id with underscores as spaces."""
    data = cast(dict[str, Any], load_raw_model(str(model_path)).data)
    out: dict[str, dict[str, str]] = {"centres": {}, "parts": {}}
    for key, section in (("centres", "locations"), ("parts", "part_types")):
        for item in cast(list[dict[str, Any]], data.get(section) or []):
            if isinstance(item, dict) and item.get("label") and item.get("name"):
                out[key][str(item["name"])] = str(item["label"])
    return out


def routes(model: CompiledModel) -> list[dict[str, Any]]:
    """G5: each finished part's work centres in order."""
    return [{"part": part, "steps": list(steps)} for part, steps in model.routing.items()]


def _order_of(lot_id: str, order_ids: Sequence[str]) -> str | None:
    if lot_id in order_ids:
        return lot_id
    for order_id in sorted(order_ids, key=len, reverse=True):
        if lot_id.startswith(order_id) and not lot_id[len(order_id)].isalnum():
            return order_id
    return None


def _rep_steps(
    events: pl.DataFrame, order_ids: Sequence[str]
) -> tuple[dict[str, dict[str, tuple[float, float]]], dict[str, tuple[float, int]]]:
    """One test run: per order and centre (work, wait) seconds; per centre (wait sum, jobs)."""
    by_order: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    by_centre: dict[str, tuple[float, int]] = {}
    for row in events.iter_rows(named=True):
        centre = str(row["location_id"])
        work = float(row["actual_end"]) - float(row["actual_start"])
        wait = max(0.0, float(row["actual_start"]) - float(row["queue_arrival_time"]))
        total, jobs = by_centre.get(centre, (0.0, 0))
        by_centre[centre] = (total + wait, jobs + 1)
        order = _order_of(str(row["lot_id"]), order_ids)
        if order is not None:
            old_work, old_wait = by_order[order].get(centre, (0.0, 0.0))
            by_order[order][centre] = (old_work + work, old_wait + wait)
    return by_order, by_centre


def steps_and_queues(
    event_paths: Sequence[Path], order_ids: Sequence[str], step_order: dict[str, int]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, float]]:
    """G3 and G4, averaged over the test runs that saw each order or centre."""
    order_sums: dict[str, dict[str, list[float]]] = defaultdict(dict)
    order_seen: dict[str, dict[str, int]] = defaultdict(dict)
    queue_sum: dict[str, float] = defaultdict(float)
    queue_jobs: dict[str, int] = defaultdict(int)
    for path in event_paths:
        events = pl.read_parquet(path).filter(pl.col("process_name") == "transform")
        by_order, by_centre = _rep_steps(events, order_ids)
        for order, centres in by_order.items():
            for centre, (work, wait) in centres.items():
                acc = order_sums[order].setdefault(centre, [0.0, 0.0])
                acc[0] += work
                acc[1] += wait
                order_seen[order][centre] = order_seen[order].get(centre, 0) + 1
        for centre, (wait, jobs) in by_centre.items():
            queue_sum[centre] += wait
            queue_jobs[centre] += jobs
    steps = {
        order: [
            {
                "centre": centre,
                "work_s": acc[0] / order_seen[order][centre],
                "wait_s": acc[1] / order_seen[order][centre],
            }
            for centre, acc in sorted(
                centres.items(), key=lambda kv: (step_order.get(kv[0], 1_000_000), kv[0])
            )
        ]
        for order, centres in order_sums.items()
    }
    queues = {c: queue_sum[c] / queue_jobs[c] for c in queue_sum if queue_jobs[c]}
    return steps, queues


def operating_hours_by_pool(model: CompiledModel, horizon: float) -> dict[str, float]:
    """On-shift hours in [0, horizon] per labor pool, from its calendar (24/7 without one)."""
    result: dict[str, float] = {}
    for pool in model.labor_pools:
        calendar = getattr(pool, "calendar", None)
        cal = WorkingCalendar(calendar) if calendar is not None else AlwaysWorkingCalendar()
        result[pool.name] = cal.available_seconds(horizon) / 3600.0
    return result


def shared_resource_rows(
    kpis: KpiSet, model: CompiledModel, horizon: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """How busy the shared machines and labor pools are: one row per machine pool and per
    labor pool, not per routing step. Each row has `util_operating` (busy over open hours,
    from the pool's shift calendar) and `util_wall` (busy over the whole run)."""
    horizon_h = horizon / 3600.0 if horizon else 0.0
    operating = operating_hours_by_pool(model, horizon)
    pool_by_skill = {skill: pool.name for pool in model.labor_pools for skill in pool.skills}

    def util(busy_h: float, denom_h: float, capacity: int) -> float:
        return busy_h / (denom_h * capacity) if denom_h and capacity else 0.0

    machines: dict[str, dict[str, Any]] = {}
    for loc in model.locations:
        entry = machines.setdefault(
            loc.machine.machine_id,
            {
                "capacity": getattr(loc, "capacity", 1),
                "steps": [],
                "busy_h": 0.0,
                "skill": getattr(loc, "labor_skill", None),
            },
        )
        entry["steps"].append(loc.location_id)
        entry["busy_h"] += kpis.busy_hours_by_location.get(loc.location_id, 0.0)
    machine_rows = []
    for machine_id, entry in sorted(machines.items()):
        pool_name = pool_by_skill.get(entry["skill"])
        operating_h = operating.get(pool_name, horizon_h) if pool_name else horizon_h
        machine_rows.append(
            {
                "resource": machine_id,
                "capacity": entry["capacity"],
                "steps": entry["steps"],
                "busy_hours": entry["busy_h"],
                "operating_hours": operating_h,
                "util_operating": util(entry["busy_h"], operating_h, entry["capacity"]),
                "util_wall": util(entry["busy_h"], horizon_h, entry["capacity"]),
            }
        )
    labor_rows = []
    for pool in model.labor_pools:
        busy_h = sum(
            h for (name, _skill), h in kpis.labor_hours_by_pool_skill.items() if name == pool.name
        )
        headcount = getattr(pool, "headcount", 1) or 1
        operating_h = operating.get(pool.name, horizon_h)
        labor_rows.append(
            {
                "resource": pool.name,
                "capacity": headcount,
                "skills": sorted(pool.skills),
                "busy_hours": busy_h,
                "operating_hours": operating_h,
                "util_operating": util(busy_h, operating_h, headcount),
                "util_wall": util(busy_h, horizon_h, headcount),
            }
        )
    return machine_rows, labor_rows


def start_instant(model: CompiledModel) -> datetime | None:
    """Wall-clock time of simulation second 0, from the first pool with a shift calendar."""
    for pool in model.labor_pools:
        origin = getattr(getattr(pool, "calendar", None), "origin", None)
        if isinstance(origin, datetime):
            return origin
    return None


def first_run(kpis: KpiSet, model: CompiledModel, horizon: float) -> dict[str, Any]:
    """The first test run's tables, the numbers the old report showed in its Detail tab."""
    wip = kpis.wip_by_location
    peaks: dict[str, int] = {}
    if wip.height:
        for row in wip.group_by("location_id").agg(pl.col("wip").max()).iter_rows(named=True):
            peaks[str(row["location_id"])] = int(row["wip"])
    cycle = {
        loc: (hours * 3600.0 / kpis.event_counts_by_location[loc])
        for loc, hours in kpis.busy_hours_by_location.items()
        if kpis.event_counts_by_location.get(loc)
    }
    machine_rows, labor_rows = shared_resource_rows(kpis, model, horizon)
    return {
        "completion_by_order": dict(kpis.completion_by_order),
        "lateness_by_order": dict(kpis.lateness_by_order),
        "on_time_pct": kpis.on_time_pct,
        "completed": kpis.completed_order_count,
        "unfinished": kpis.censored_order_count,
        "utilization_by_cell": dict(kpis.utilization_by_cell),
        "wait_seconds_by_location": {k: dict(v) for k, v in kpis.wait_seconds_by_location.items()},
        "machine_hours_by_machine": dict(kpis.machine_hours_by_machine),
        "labor_hours": [
            {"pool": pool, "skill": skill, "hours": hours}
            for (pool, skill), hours in sorted(kpis.labor_hours_by_pool_skill.items())
        ],
        "run_hours": kpis.run_hours,
        "setup_hours": kpis.setup_hours,
        "event_counts_by_location": dict(kpis.event_counts_by_location),
        "cycle_seconds_by_location": cycle,
        "wip_peak_by_location": peaks,
        "wip": [
            {"location_id": str(r["location_id"]), "t": float(r["t"]), "wip": int(r["wip"])}
            for r in wip.sort("t").iter_rows(named=True)
        ],
        "machine_pools": machine_rows,
        "labor_pools": labor_rows,
    }


def per_rep_outcomes(per_rep: Sequence[KpiSet]) -> dict[str, Any]:
    """G2: each order's due state in every test run, and late orders per run."""
    states: dict[str, list[str]] = defaultdict(list)
    for kpis in per_rep:
        for order, outcome in kpis.order_outcomes.items():
            states[order].append(outcome.due_state)
    return {
        "due_state_by_order": dict(states),
        "lateness_by_order": [dict(k.lateness_by_order) for k in per_rep],
        "late_orders": [
            sum(o.due_state == "late" for o in k.order_outcomes.values()) for k in per_rep
        ],
        "unfinished_orders": [k.censored_order_count for k in per_rep],
        "blocked_starved_by_location": _mean_waits(per_rep),
    }


def _mean_waits(per_rep: Sequence[KpiSet]) -> dict[str, dict[str, float]]:
    sums: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for kpis in per_rep:
        for loc, waits in kpis.wait_seconds_by_location.items():
            for kind, seconds in waits.items():
                sums[loc][kind] += seconds
    n = len(per_rep) or 1
    return {loc: {k: v / n for k, v in kinds.items()} for loc, kinds in sums.items()}


def shown_path(path: str) -> str:
    """A path as a person would type it: relative to the working folder when inside it."""
    try:
        return str(Path(path).resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return path


def write_run_detail(
    run_dir: Path,
    *,
    model: CompiledModel,
    work_orders: Sequence[WorkOrder],
    per_rep: Sequence[KpiSet],
    event_paths: Sequence[Path],
    horizon: float,
    assumptions: Sequence[Assumption],
    model_path: str,
    plan_path: str,
    reps: int,
) -> None:
    """Write `plan.json` (G1) and `run_detail.json` into `run_dir`."""
    rows = plan_rows(work_orders)
    (run_dir / PLAN_FILE).write_text(
        json.dumps({"schema_version": 1, "orders": rows}, indent=2), encoding="utf-8"
    )
    order_ids = [r["id"] for r in rows]
    route_list = routes(model)
    step_order: dict[str, int] = {}
    for route in route_list:
        for i, step in enumerate(route["steps"]):
            step_order.setdefault(step, i)
    steps, queues = steps_and_queues(event_paths, order_ids, step_order)
    start = start_instant(model)
    detail = {
        "schema_version": DETAIL_SCHEMA_VERSION,
        "project": Path(model_path).resolve().parent.name,
        "model_path": shown_path(model_path),
        "plan_path": shown_path(plan_path),
        "reps": reps,
        "horizon_s": horizon,
        "start": start.isoformat() if start else None,
        "labels": labels(model_path),
        "routes": route_list,
        "capacity": {loc.location_id: getattr(loc, "capacity", 1) for loc in model.locations},
        "per_rep": per_rep_outcomes(per_rep),
        "steps": steps,
        "queue_wait_s": queues,
        "first_run": first_run(per_rep[0], model, horizon),
        "assumptions": [asdict(a) for a in assumptions],
    }
    (run_dir / DETAIL_FILE).write_text(json.dumps(detail, indent=1), encoding="utf-8")


__all__ = [
    "DETAIL_FILE",
    "PLAN_FILE",
    "first_run",
    "labels",
    "operating_hours_by_pool",
    "plan_rows",
    "routes",
    "shared_resource_rows",
    "steps_and_queues",
    "write_run_detail",
]
