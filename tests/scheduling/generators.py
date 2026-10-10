"""Seeded problem generators for the property and scale suites (T-104, T-111, COMP-012).

`random_request(seed)` builds a small valid `SolveRequest` (3-8 jobs, 1-4 operations, 2-5
machines) from `np.random.default_rng(seed)`. Seeds rotate through three shapes:

- seed % 3 == 0: inside CP-SAT's capabilities (no setups, batching, pause, overlap or
  detached setup), integer times and a makespan or max-lateness objective, so CP-SAT runs;
- otherwise: every feature the baseline supports, including the three setup term kinds
  (abs_diff, mismatch, family matrix), batching, pause and transfer-lot overlap.

Some rich seeds (seed % 10 == 7) add a cross-job link plus split case (T-115, D-084): one
untouched job's first operation waits on another untouched job's last operation (sometimes
through a transfer lot), and the predecessor job is split. Its draws come from a separate
stream, `np.random.default_rng((seed, 115))`, so every other seed's request is unchanged.

Restart rules (D-074, T-117): a few seeds (seed % 20 == 13) put one on the last machine,
and rich seeds give each machine, the batch machine included, a rule with some chance
(short thresholds, so warm-ups actually happen). The rich-seed draws come from a separate
stream, `np.random.default_rng((seed, 117))`, so no other draw of any seed changes. The
baseline supports restart; CP-SAT refuses it until T-122. Overrides of every kind and a
frozen zone with a previous plan appear on some seeds. Override targets are distinct jobs
without in-progress operations so the compiled problem stays valid.

`scale_request(seed)` builds the 600 jobs x 8 operations problem of T-111.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

import numpy as np

from twinflow.scheduling.model import SolveRequest

T = TypeVar("T")

WC = "WC"
POOL = "P"
BATCH_MACHINE = "B1"
CALENDAR = [(k * 120.0, k * 120.0 + 100.0) for k in range(100)]
POOL_CALENDAR = [(k * 120.0 + 10.0, k * 120.0 + 110.0) for k in range(100)]
SETUP_FAMILIES = ("A", "B")
OVERRIDE_KINDS = (
    "hold",
    "split",
    "priority",
    "expedite",
    "exclude_machine",
    "prefer_machine",
    "lock",
)
CPSAT_OBJECTIVES = ("makespan", "max_lateness")
RESTART_THRESHOLDS = (5.0, 20.0, 60.0)
RESTART_WARMUPS = (2.0, 5.0, 10.0)
FULL_OBJECTIVES = ("makespan", "weighted_tardiness", "total_setup", "cost")


def _int(rng: np.random.Generator, low: int, high: int) -> int:
    """Uniform integer in [low, high]."""
    return int(rng.integers(low, high + 1))


def _pick(rng: np.random.Generator, items: Sequence[T]) -> T:
    return items[int(rng.integers(len(items)))]


def _chance(rng: np.random.Generator, p: float) -> bool:
    return bool(rng.random() < p)


@dataclass(frozen=True)
class Shape:
    """Which feature families a seed may use."""

    seed: int
    cpsat_friendly: bool
    restart: bool

    @classmethod
    def for_seed(cls, seed: int) -> Shape:
        return cls(seed=seed, cpsat_friendly=seed % 3 == 0, restart=seed % 20 == 13)

    @property
    def cross_job_split(self) -> bool:
        """A cross-job link into a split predecessor job is added (T-115)."""
        return self.rich and self.seed % 10 == 7

    @property
    def rich(self) -> bool:
        """Setups, batching, pause, overlap and detached setup are allowed."""
        return not self.cpsat_friendly


# --- small random requests ------------------------------------------------------------------


class _RequestBuilder:
    """Builds one request as plain data, then validates it into a `SolveRequest`."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = np.random.default_rng(seed)
        self.shape = Shape.for_seed(seed)
        self.machine_ids = [f"M{m}" for m in range(_int(self.rng, 2, 5))]
        self.jobs: list[dict[str, Any]] = []
        self.running_machines: set[str] = set()

    def build(self) -> SolveRequest:
        for j in range(_int(self.rng, 3, 8)):
            self.jobs.append(self._job(j))
        if self.shape.rich and _chance(self.rng, 0.6):
            self.jobs += [self._batch_job(len(self.jobs) + b) for b in range(_int(self.rng, 1, 2))]
        data: dict[str, Any] = {
            "schema_version": "2.0",
            "problem": self._problem(),
            "profile": self._profile(),
        }
        if _chance(self.rng, 0.7):
            data["overrides"] = self._overrides()
        if _chance(self.rng, 0.3):
            data["previous_plan"] = self._previous_plan()
            data["profile"]["frozen_zone"] = {
                "until": 100.0,
                "tolerance": _pick(self.rng, (30.0, 1000.0)),
            }
        if self.shape.cross_job_split:
            self._add_cross_job_split(data)
        return SolveRequest.model_validate(data)

    # --- jobs and operations --------------------------------------------------------------

    def _job(self, j: int) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        for k in range(_int(self.rng, 1, 4)):
            op = self._operation(f"J{j}-{k}")
            if operations:
                op["predecessors"] = [self._link(operations[-1], op)]
            operations.append(op)
        if j < len(self.machine_ids) and _chance(self.rng, 0.25):
            self._start_running(operations[0], self.machine_ids[j])
        elif _chance(self.rng, 0.05):
            operations[0]["status"] = {"state": "done"}
        job: dict[str, Any] = {
            "id": f"J{j}",
            "release": float(_pick(self.rng, (0, 0, 15))),
            "weight_class": _pick(self.rng, (1, 2, 4)),
            "operations": operations,
        }
        if _chance(self.rng, 0.6):
            job["due"] = float(_int(self.rng, 50, 400))
        return job

    def _operation(self, op_id: str) -> dict[str, Any]:
        rng = self.rng
        machines = [str(m) for m in rng.permutation(self.machine_ids)[: _int(rng, 1, 3)]]
        op: dict[str, Any] = {
            "id": op_id,
            "work_center_id": WC,
            "quantity": float(_int(rng, 1, 4)),
            "options": [self._option(m) for m in machines],
            "ready_time": float(_pick(rng, (0, 0, 20))),
        }
        if self.shape.rich:
            op |= self._rich_operation_fields()
        if _chance(rng, 0.3):
            phase = _pick(rng, ("setup", "run", "both"))
            op["operator_needs"] = [{"pool_id": POOL, "quantity": 1, "phase": phase}]
        if _chance(rng, 0.12):
            op["pin"] = self._pin(machines)
        return op

    def _rich_operation_fields(self) -> dict[str, Any]:
        rng = self.rng
        return {
            "setup_params": {"w": float(_int(rng, 0, 5)), "c": _pick(rng, "xy")},
            "setup_family": _pick(rng, SETUP_FAMILIES),
            "setup_detached": _chance(rng, 0.3),
            "shift_crossing": "pause" if _chance(rng, 0.25) else "forbid",
        }

    def _option(self, machine_id: str) -> dict[str, Any]:
        return {
            "machine_id": machine_id,
            "fixed_seconds": float(_int(self.rng, 1, 10)),
            "unit_seconds": float(_int(self.rng, 0, 5)),
            "preference_cost": float(_int(self.rng, 0, 3)),
        }

    def _pin(self, machines: Sequence[str]) -> dict[str, Any]:
        earliest = float(_int(self.rng, 0, 100))
        choices: tuple[dict[str, Any], ...] = (
            {"machine_id": machines[0]},
            {"earliest_start": earliest},
            {"earliest_start": earliest, "latest_start": earliest + 500.0},
        )
        return _pick(self.rng, choices)

    def _link(self, pred: dict[str, Any], succ: dict[str, Any]) -> dict[str, Any]:
        rng = self.rng
        link: dict[str, Any] = {"operation_id": pred["id"]}
        if _chance(rng, 0.3):
            link["min_lag"] = float(_int(rng, 1, 10))
        if _chance(rng, 0.2):
            link["max_lag"] = link.get("min_lag", 0.0) + float(_int(rng, 50, 300))
        both_forbid = "pause" not in (pred.get("shift_crossing"), succ.get("shift_crossing"))
        if self.shape.rich and both_forbid and _chance(rng, 0.25):
            link["overlap_quantity"] = float(_int(rng, 1, int(pred["quantity"])))
        return link

    def _start_running(self, op: dict[str, Any], machine_id: str) -> None:
        """Make `op` in progress on `machine_id`; one in-progress op per machine."""
        if machine_id not in {option["machine_id"] for option in op["options"]}:
            op["options"].append(self._option(machine_id))
        remaining = float(_int(self.rng, 1, 30))
        op["status"] = {
            "state": "in_progress",
            "machine_id": machine_id,
            "remaining_seconds": remaining,
        }
        op.pop("pin", None)
        op.pop("operator_needs", None)
        self.running_machines.add(machine_id)

    def _batch_job(self, j: int) -> dict[str, Any]:
        option = {
            "machine_id": BATCH_MACHINE,
            "fixed_seconds": 10.0,
            "unit_seconds": 0.0,
            "preference_cost": 0.0,
        }
        op = {
            "id": f"J{j}-0",
            "work_center_id": WC,
            "quantity": float(_int(self.rng, 1, 2)),
            "options": [option],
            "batch_family": _pick(self.rng, "FG"),
        }
        return {"id": f"J{j}", "release": float(_pick(self.rng, (0, 5))), "operations": [op]}

    # --- resources ------------------------------------------------------------------------

    def _problem(self) -> dict[str, Any]:
        problem: dict[str, Any] = {
            "id": f"prop-{self.seed}",
            "jobs": self.jobs,
            "machines": self._machines(),
            "work_centers": [{"id": WC}],
            "calendars": [
                {"id": "C", "intervals": CALENDAR},
                {"id": "PC", "intervals": POOL_CALENDAR},
            ],
            "operator_pools": [self._pool()],
        }
        if self.shape.rich:
            problem["work_centers"] = [{"id": WC, "setup_function_id": "S_SUM"}]
            problem["setup_functions"] = _setup_functions()
        return problem

    def _machines(self) -> list[dict[str, Any]]:
        machines = [self._machine(machine_id) for machine_id in self.machine_ids]
        if self.shape.rich and _chance(self.rng, 0.5):
            machines[0]["setup_function_id"] = "S_MAX"
        if self.shape.restart:
            machines[-1]["restart"] = {"idle_threshold_seconds": 60.0, "warmup_seconds": 5.0}
        if any(BATCH_MACHINE == op["options"][0]["machine_id"] for op in self._operations()):
            batch = {"capacity": 3.0, "unit": "pc"}
            machines.append({"id": BATCH_MACHINE, "work_center_id": WC, "batch": batch})
        if self.shape.rich:
            self._add_restart_rules(machines)
        return machines

    def _add_restart_rules(self, machines: list[dict[str, Any]]) -> None:
        """Give each machine a restart rule with chance 0.3, from its own stream (T-117)."""
        rng = np.random.default_rng((self.seed, 117))
        for machine in machines:
            if _chance(rng, 0.3):
                machine["restart"] = {
                    "idle_threshold_seconds": _pick(rng, RESTART_THRESHOLDS),
                    "warmup_seconds": _pick(rng, RESTART_WARMUPS),
                }

    def _machine(self, machine_id: str) -> dict[str, Any]:
        rng = self.rng
        machine: dict[str, Any] = {"id": machine_id, "work_center_id": WC}
        if _chance(rng, 0.5):
            machine["calendar_id"] = "C"
        state: dict[str, Any] = {}
        if machine_id not in self.running_machines and _chance(rng, 0.3):
            state["available_from"] = 30.0
        if self.shape.rich and _chance(rng, 0.5):
            state["last_setup_params"] = {"w": float(_int(rng, 0, 5)), "c": "x"}
            state["last_setup_family"] = _pick(rng, SETUP_FAMILIES)
        if state:
            machine["state"] = state
        return machine

    def _pool(self) -> dict[str, Any]:
        pool: dict[str, Any] = {"id": POOL, "size": _int(self.rng, 1, 2)}
        if _chance(self.rng, 0.5):
            pool["calendar_id"] = "PC"
        return pool

    def _profile(self) -> dict[str, Any]:
        kinds = CPSAT_OBJECTIVES if self.shape.cpsat_friendly else FULL_OBJECTIVES
        kind = _pick(self.rng, kinds)
        if self.shape.rich and _chance(self.rng, 0.5):
            objective = {"mode": "weighted_sum", "terms": [{"kind": kind, "weight": 1.0}]}
        else:
            objective = {"mode": "lexicographic", "levels": [{"kind": kind}]}
        return {"objective": objective}

    # --- overrides and previous plan ------------------------------------------------------

    def _operations(self) -> list[dict[str, Any]]:
        return [op for job in self.jobs for op in job["operations"]]

    def _override_targets(self) -> list[dict[str, Any]]:
        """Jobs safe to override: no in-progress operation, in a seeded order."""
        safe = [
            job
            for job in self.jobs
            if all(op.get("status", {}).get("state") != "in_progress" for op in job["operations"])
        ]
        return [safe[int(i)] for i in self.rng.permutation(len(safe))]

    def _overrides(self) -> list[dict[str, Any]]:
        targets = self._override_targets()
        kinds = [str(kind) for kind in self.rng.permutation(OVERRIDE_KINDS)]
        overrides = []
        for index, (kind, job) in enumerate(zip(kinds, targets, strict=False)):
            if _chance(self.rng, 0.6):
                override = _OVERRIDE_BUILDERS[kind](self, job)
                override |= {"id": f"o{index}", "fact_ref": "fact", "reason": "test"}
                overrides.append(override)
        return overrides

    def _add_cross_job_split(self, data: dict[str, Any]) -> None:
        """Link two plain jobs no override touches, then split the predecessor job (T-115).

        Plain: no status, pin or batch family, so the link cannot meet a done, running or
        pinned operation. No max_lag: the successor waits on every child, and a max_lag
        window across children on shared machines can be infeasible by construction.
        """
        rng = np.random.default_rng((self.seed, 115))
        overrides: list[dict[str, Any]] = data.setdefault("overrides", [])
        candidates = [job for job in self.jobs if self._plain_untargeted(job, overrides)]
        if len(candidates) < 2:
            return
        first, second = (candidates[int(i)] for i in rng.permutation(len(candidates))[:2])
        pred, succ = first["operations"][-1], second["operations"][0]
        link: dict[str, Any] = {"operation_id": pred["id"], "min_lag": float(_int(rng, 0, 10))}
        if "pause" not in (pred["shift_crossing"], succ["shift_crossing"]) and _chance(rng, 0.7):
            link["overlap_quantity"] = float(_int(rng, 1, int(pred["quantity"])))
        succ.setdefault("predecessors", []).append(link)
        fractions = _pick(rng, ((0.5, 0.5), (0.25, 0.75), (0.25, 0.25, 0.5)))
        split = {"kind": "split", "hardness": "hard", "job_id": first["id"], "fractions": fractions}
        overrides.append(split | {"id": "o115", "fact_ref": "fact", "reason": "test"})

    def _plain_untargeted(self, job: dict[str, Any], overrides: list[dict[str, Any]]) -> bool:
        op_ids = {op["id"] for op in job["operations"]}
        targeted = any(
            override.get("job_id") == job["id"] or override.get("operation_id") in op_ids
            for override in overrides
        )
        plain = all(not {"status", "pin", "batch_family"} & op.keys() for op in job["operations"])
        return plain and not targeted

    def _previous_plan(self) -> dict[str, Any]:
        assignments = [
            {
                "operation_id": op["id"],
                "machine_id": _pick(self.rng, op["options"])["machine_id"],
                "start": float(_int(self.rng, 0, 200)),
            }
            for op in self._operations()
            if "status" not in op and _chance(self.rng, 0.5)
        ]
        return {"plan_id": "prev", "problem_digest": "digest", "assignments": assignments}


def _hold(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    until = _pick(builder.rng, (None, 50.0))
    return {"kind": "hold", "hardness": "hard", "job_id": job["id"], "until": until}


def _split(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    fractions = _pick(builder.rng, ((0.5, 0.5), (0.25, 0.75)))
    return {"kind": "split", "hardness": "hard", "job_id": job["id"], "fractions": fractions}


def _priority(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    override = {"kind": "priority", "hardness": "hard", "job_id": job["id"]}
    if _chance(builder.rng, 0.5):
        return override | {"weight_class": _pick(builder.rng, (1, 2, 4))}
    return override | {"weight": 3.0}


def _expedite(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    due = _pick(builder.rng, (None, 120.0))
    return {"kind": "expedite", "hardness": "hard", "job_id": job["id"], "due": due}


def _machine_override(builder: _RequestBuilder, job: dict[str, Any], kind: str) -> dict[str, Any]:
    """Hard on one multi-option operation without a machine pin; otherwise soft.

    A hard removal of the machine an input pin names is a contradiction the solve refuses
    with `pin_outside_options`, so the generator does not build it.
    """
    rng = builder.rng
    op = _pick(rng, job["operations"])
    machine_id = _pick(rng, op["options"])["machine_id"]
    override: dict[str, Any] = {"kind": kind, "machine_id": machine_id}
    hard_ok = len(op["options"]) > 1 and "machine_id" not in op.get("pin", {})
    if hard_ok and _chance(rng, 0.5):
        return override | {"hardness": "hard", "operation_id": op["id"]}
    target = {"operation_id": op["id"]} if _chance(rng, 0.5) else {"job_id": job["id"]}
    return override | target | {"hardness": "soft", "penalty": 5.0}


def _exclude(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    return _machine_override(builder, job, "exclude_machine")


def _prefer(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    return _machine_override(builder, job, "prefer_machine")


def _lock(builder: _RequestBuilder, job: dict[str, Any]) -> dict[str, Any]:
    rng = builder.rng
    op = _pick(rng, job["operations"])
    lock: dict[str, Any] = {"kind": "lock", "hardness": "hard", "operation_id": op["id"]}
    if _chance(rng, 0.7):
        lock["machine_id"] = _pick(rng, op["options"])["machine_id"]
    if _chance(rng, 0.3) or "machine_id" not in lock:
        lock["start"] = 300.0
    return lock


_OVERRIDE_BUILDERS = {
    "hold": _hold,
    "split": _split,
    "priority": _priority,
    "expedite": _expedite,
    "exclude_machine": _exclude,
    "prefer_machine": _prefer,
    "lock": _lock,
}


def _setup_functions() -> list[dict[str, Any]]:
    """A summed function with all three term kinds and a max-combined one (D-064)."""
    family_matrix = [
        {"from_family": "A", "to_family": "B", "seconds": 7.0},
        {"from_family": "B", "to_family": "A", "seconds": 5.0},
    ]
    return [
        {
            "id": "S_SUM",
            "terms": [
                {"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0},
                {"kind": "mismatch", "param": "c", "seconds": 3.0},
            ],
            "family_matrix": family_matrix,
            "default_seconds": 2.0,
        },
        {
            "id": "S_MAX",
            "terms": [{"kind": "mismatch", "param": "c", "seconds": 4.0}],
            "combine": "max",
            "family_matrix": family_matrix,
        },
    ]


def random_request(seed: int) -> SolveRequest:
    """A small valid request from `seed`; the same seed always gives the same request."""
    return _RequestBuilder(seed).build()


# --- the T-111 scale problem ----------------------------------------------------------------


SCALE_JOBS = 600
SCALE_OPERATIONS = 8
SCALE_MACHINES_PER_STEP = 5
SCALE_CALENDAR = [(k * 3600.0, k * 3600.0 + 3000.0) for k in range(2000)]


def _scale_operation(rng: np.random.Generator, op_id: str, step: int) -> dict[str, Any]:
    first = step * SCALE_MACHINES_PER_STEP
    group = [f"M{m}" for m in range(first, first + SCALE_MACHINES_PER_STEP)]
    machines = [str(m) for m in rng.permutation(group)[:2]]
    op: dict[str, Any] = {
        "id": op_id,
        "work_center_id": WC,
        "quantity": float(_int(rng, 1, 10)),
        "options": [
            {
                "machine_id": m,
                "fixed_seconds": float(_int(rng, 60, 600)),
                "unit_seconds": float(_int(rng, 5, 60)),
                "preference_cost": 0.0,
            }
            for m in machines
        ],
        "setup_params": {"w": float(_int(rng, 0, 5)), "c": _pick(rng, "xyz")},
    }
    if _chance(rng, 0.3):
        op["operator_needs"] = [{"pool_id": POOL, "quantity": 1, "phase": "setup"}]
    return op


def _scale_job(rng: np.random.Generator, j: int) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for k in range(SCALE_OPERATIONS):
        op = _scale_operation(rng, f"J{j}-{k}", k)
        if operations:
            op["predecessors"] = [{"operation_id": operations[-1]["id"]}]
        operations.append(op)
    release = float(_int(rng, 0, 20_000))
    return {"id": f"J{j}", "release": release, "due": 200_000.0, "operations": operations}


def scale_request(seed: int = 0) -> SolveRequest:
    """600 jobs x 8 operations on 40 calendared machines with setups and an operator pool."""
    rng = np.random.default_rng(seed)
    machine_count = SCALE_OPERATIONS * SCALE_MACHINES_PER_STEP
    setup_terms = [
        {"kind": "abs_diff", "param": "w", "seconds_per_unit": 30.0},
        {"kind": "mismatch", "param": "c", "seconds": 120.0},
    ]
    problem = {
        "id": f"scale-{seed}",
        "jobs": [_scale_job(rng, j) for j in range(SCALE_JOBS)],
        "machines": [
            {"id": f"M{m}", "work_center_id": WC, "calendar_id": "C"} for m in range(machine_count)
        ],
        "work_centers": [{"id": WC, "setup_function_id": "S"}],
        "calendars": [{"id": "C", "intervals": SCALE_CALENDAR}],
        "operator_pools": [{"id": POOL, "size": 6}],
        "setup_functions": [{"id": "S", "terms": setup_terms}],
    }
    return SolveRequest.model_validate({"schema_version": "2.0", "problem": problem})
