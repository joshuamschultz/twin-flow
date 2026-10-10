"""Seeded HMLV job-shop instances for the quality gates (T-127, COMP-027, REQ-125, D-083).

`hmlv_instance(seed, ...)` follows the SDD Data Model "Gate definition": `jobs` jobs of
`ops_per_job` chained operations over `centers` work centers of `machines_per_center`
machines, drawn from `np.random.default_rng(seed)`:

- each operation has 1 to 3 eligible machines of its center; one base time (fixed 30-120 s,
  unit 10-60 s, quantity 1-10) is divided by a per-option speed in [0.8, 1.25], and each
  option carries a preference cost 0, 1, 2 or 3;
- setups: one function `S` on every center, summing a wire-size `abs_diff` term (param
  `wire` in 0..9) and a family matrix over families F0-F2. Setup severity eta sets the mean
  setup to about eta times the mean run time: half from the wire term (mean |a - b| of two
  uniform draws from 0..9 is 3.3), half from the matrix (off-diagonal entries uniform in
  [0.5, 1.0] x eta x mean run, mean 0.75, two thirds of family pairs differ);
- weight classes 1, 2 and 4 with probabilities 0.6, 0.3 and 0.1; every release is 0;
- calendars on half the machines (`calendars=False` turns them off): 3000 s working in
  every 3600 s, enough intervals to run every operation serially on one machine;
- due dates (Pinedo's generator): d_j uniform in [Cmax (1 - tau - R/2), Cmax (1 - tau +
  R/2)], Cmax from `estimated_makespan` (the busiest center's mean load times (1 + eta)
  over its machine count);
- `flow_line=False` sends each operation to a random center; `flow_line=True` sends
  operation k of every job to center k, so every job visits the centers in the same order
  (it needs `ops_per_job <= centers`).

Training seeds are 0-19 and held-out seeds 100-129 (D-083). `tiny_instance` is the 3 jobs x
2 ops x 2 centers x 2 machines mode without calendars that the brute-force oracle can solve.
`scale_instance` is the 600 jobs x 8 ops problem built with `hmlv_instance` (the TF-002
`generators.scale_request` has one work center and one shared due date, so it has no
tardiness to trade against setups).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from twinflow.scheduling.model import Problem, SolveRequest

TRAINING_SEEDS = tuple(range(20))
HELD_OUT_SEEDS = tuple(range(100, 130))
WEIGHT_CLASSES = (1, 2, 4)
WEIGHT_SHARES = (0.6, 0.3, 0.1)
FAMILIES = ("F0", "F1", "F2")
WIRE_SIZES = 10
MEAN_WIRE_GAP = (WIRE_SIZES**2 - 1) / (3 * WIRE_SIZES)
DIFFERENT_FAMILY_SHARE = 2 / 3
MIN_SPEED, MAX_SPEED = 0.8, 1.25
MAX_OPTIONS = 3
MAX_PREFERENCE_COST = 3
SHIFT_SECONDS, SHIFT_WORKING_SECONDS = 3600.0, 3000.0
SETUP_FUNCTION = "S"
CALENDAR = "SHIFTS"


def _uniform(rng: np.random.Generator, low: float, high: float) -> float:
    return float(rng.uniform(low, high))


def _int(rng: np.random.Generator, low: int, high: int) -> int:
    """Uniform integer in [low, high]."""
    return int(rng.integers(low, high + 1))


class _Builder:
    """Builds one instance as plain data, then validates it into a `SolveRequest`."""

    def __init__(self, seed: int, centers: int, machines_per_center: int) -> None:
        self.rng = np.random.default_rng(seed)
        self.centers = [f"C{c}" for c in range(centers)]
        self.machines = {
            center: [f"{center}M{m}" for m in range(machines_per_center)] for center in self.centers
        }

    def job(self, j: int, ops: int, flow_line: bool) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        for k in range(ops):
            center = (
                self.centers[k]
                if flow_line
                else self.centers[_int(self.rng, 0, len(self.centers) - 1)]
            )
            op = self.operation(f"J{j}-{k}", center)
            if operations:
                op["predecessors"] = [{"operation_id": operations[-1]["id"]}]
            operations.append(op)
        weight_class = int(self.rng.choice(WEIGHT_CLASSES, p=WEIGHT_SHARES))
        return {"id": f"J{j}", "weight_class": weight_class, "operations": operations}

    def operation(self, op_id: str, center: str) -> dict[str, Any]:
        rng = self.rng
        pool = self.machines[center]
        eligible = rng.permutation(pool)[: _int(rng, 1, min(MAX_OPTIONS, len(pool)))]
        fixed, unit = _uniform(rng, 30.0, 120.0), _uniform(rng, 10.0, 60.0)
        return {
            "id": op_id,
            "work_center_id": center,
            "quantity": float(_int(rng, 1, 10)),
            "options": [self.option(str(m), fixed, unit) for m in eligible],
            "setup_params": {"wire": float(_int(rng, 0, WIRE_SIZES - 1))},
            "setup_family": FAMILIES[_int(rng, 0, len(FAMILIES) - 1)],
        }

    def option(self, machine_id: str, fixed: float, unit: float) -> dict[str, Any]:
        speed = _uniform(self.rng, MIN_SPEED, MAX_SPEED)
        return {
            "machine_id": machine_id,
            "fixed_seconds": round(fixed / speed, 2),
            "unit_seconds": round(unit / speed, 2),
            "preference_cost": float(_int(self.rng, 0, MAX_PREFERENCE_COST)),
        }

    def setup_function(self, severity: float, mean_run: float) -> dict[str, Any]:
        half = severity * mean_run / 2
        matrix = [
            {
                "from_family": a,
                "to_family": b,
                "seconds": round(
                    _uniform(self.rng, 0.5, 1.0) * half / (0.75 * DIFFERENT_FAMILY_SHARE), 2
                ),
            }
            for a in FAMILIES
            for b in FAMILIES
            if a != b
        ]
        wire = {"kind": "abs_diff", "param": "wire", "seconds_per_unit": half / MEAN_WIRE_GAP}
        return {"id": SETUP_FUNCTION, "terms": [wire], "family_matrix": matrix}

    def machine_rows(self, calendars: bool) -> list[dict[str, Any]]:
        ids = [m for center in self.centers for m in self.machines[center]]
        calendared = set(self.rng.permutation(ids)[: len(ids) // 2]) if calendars else set()
        rows = []
        for center in self.centers:
            for machine_id in self.machines[center]:
                row: dict[str, Any] = {"id": machine_id, "work_center_id": center}
                if machine_id in calendared:
                    row["calendar_id"] = CALENDAR
                rows.append(row)
        return rows


def _run_seconds(op: dict[str, Any]) -> list[float]:
    q = op["quantity"]
    return [o["fixed_seconds"] + q * o["unit_seconds"] for o in op["options"]]


def _shift_calendar(jobs: list[dict[str, Any]], max_setup: float) -> dict[str, Any]:
    """Enough shifts to run every operation serially on one machine, plus slack."""
    ops = [op for job in jobs for op in job["operations"]]
    serial = sum(max(_run_seconds(op)) + max_setup for op in ops)
    count = math.ceil(serial / SHIFT_WORKING_SECONDS) + 2
    intervals = [
        (k * SHIFT_SECONDS, k * SHIFT_SECONDS + SHIFT_WORKING_SECONDS) for k in range(count)
    ]
    return {"id": CALENDAR, "intervals": intervals}


def estimated_makespan(problem: Problem, setup_severity: float) -> float:
    """Cmax estimate: max over centers of sum(mean option run) x (1 + eta) / machine count."""
    machines: dict[str, int] = {}
    for machine in problem.machines:
        machines[machine.work_center_id] = machines.get(machine.work_center_id, 0) + 1
    load: dict[str, float] = {}
    for job in problem.jobs:
        for op in job.operations:
            runs = [o.fixed_seconds + op.quantity * o.unit_seconds for o in op.options]
            load[op.work_center_id] = load.get(op.work_center_id, 0.0) + sum(runs) / len(runs)
    return max(load[c] * (1 + setup_severity) / machines[c] for c in load)


def _with_due_dates(
    problem: Problem, rng: np.random.Generator, severity: float, tau: float, spread: float
) -> Problem:
    cmax = estimated_makespan(problem, severity)
    low, high = cmax * (1 - tau - spread / 2), cmax * (1 - tau + spread / 2)
    jobs = tuple(
        job.model_copy(update={"due": round(_uniform(rng, low, high), 2)}) for job in problem.jobs
    )
    return problem.model_copy(update={"jobs": jobs})


def hmlv_instance(
    seed: int,
    *,
    jobs: int = 40,
    ops_per_job: int = 5,
    centers: int = 4,
    machines_per_center: int = 3,
    setup_severity: float = 0.5,
    tightness: float = 0.5,
    due_range: float = 0.5,
    flow_line: bool = False,
    calendars: bool = True,
) -> SolveRequest:
    """A seeded HMLV request; the same arguments always give the same request (module doc)."""
    if flow_line and ops_per_job > centers:
        raise ValueError(f"flow_line needs ops_per_job <= centers, got {ops_per_job} > {centers}")
    builder = _Builder(seed, centers, machines_per_center)
    job_rows = [builder.job(j, ops_per_job, flow_line) for j in range(jobs)]
    runs = [r for job in job_rows for op in job["operations"] for r in _run_seconds(op)]
    setup = builder.setup_function(setup_severity, sum(runs) / len(runs))
    max_setup = (WIRE_SIZES - 1) * setup["terms"][0]["seconds_per_unit"] + max(
        entry["seconds"] for entry in setup["family_matrix"]
    )
    problem_data = {
        "id": f"hmlv-{seed}",
        "jobs": job_rows,
        "machines": builder.machine_rows(calendars),
        "work_centers": [{"id": c, "setup_function_id": SETUP_FUNCTION} for c in builder.centers],
        "calendars": [_shift_calendar(job_rows, max_setup)] if calendars else [],
        "setup_functions": [setup],
    }
    problem = Problem.model_validate(problem_data)
    problem = _with_due_dates(problem, builder.rng, setup_severity, tightness, due_range)
    return SolveRequest(schema_version="2.0", problem=problem)


def tiny_instance(seed: int) -> SolveRequest:
    """3 jobs x 2 ops on 2 centers of 2 machines, no calendars: small enough to brute-force."""
    return hmlv_instance(
        seed, jobs=3, ops_per_job=2, centers=2, machines_per_center=2, calendars=False
    )


def scale_instance(seed: int = 0) -> SolveRequest:
    """600 jobs x 8 ops on 8 centers of 5 machines (T-111 size, HMLV data)."""
    return hmlv_instance(seed, jobs=600, ops_per_job=8, centers=8, machines_per_center=5)
