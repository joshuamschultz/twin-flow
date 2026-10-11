"""Small verified v2 problems and plans, one generator per feature group (T-172, COMP-142).

Every `generate_<group>(seed)` returns a `GeneratedPlan(request, compiled, schedule)` whose
schedule the TF-002 baseline produced and the independent verifier accepted, both inside
`solve` (`result.verified`) and again exactly as the simulation service will re-verify it
(`verify(compiled, overrides, schedule with primary_value=None, previous_plan)`, COMP-139).

Groups (REQ-129, REQ-130, REQ-131):

- core: unary machines, alternatives and a cross-center substitute, end-to-gate precedence,
  job release, op ready_time, machine available_from; no calendars, setups or operators.
- calendars: machine calendars with breaks and priced overtime; every `shift_crossing`.
- setups: `abs_diff`, `mismatch` and family-matrix setups, a machine-level function beside
  the work-center one, detached setups, machine state (last params/family, available_from).
- operators: pools held by phase (setup, run, both), pool calendars narrower than the
  machine calendars, pools of size 1.
- batches: machines with a `BatchCapacity`, batch-family operations, one shared setup.
- precedence: min lags, max lags, transfer-lot overlap, pins with windows and machines, a
  non-linear precedence DAG of lots inside one job.
- state: in-progress ops, a paused op (pending, one option, hard lock, TF-005 ruling B),
  held jobs by status and by `HoldOverride` without until, hold until, done ops and done
  jobs, split children (`SplitOverride`, TF-003 D-084 ids `X~k`), material ready time.
- restart: machines with a `RestartRule` (TF-003 D-074). The baseline refuses `restart`,
  so this group is solved by the TF-003 portfolio only; `generate_restart` raises
  `Tf003Unavailable` when it is absent and callers skip.
- mixed: every part above (except restart) in one problem, with cross-part links.

Builders take a namespacing prefix and `now`, so `mixed` is the union of the group parts.
A seed that does not verify is regenerated from the next deterministic sub-seed
(`(group index, seed, attempt)`); after `MAX_ATTEMPTS` the generator raises, so a group is
never silently dropped. Times are float seconds; calendars are absolute from 0 and long
enough that no plan reaches their end.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

import numpy as np

from twinflow.application.portfolio_port import load_portfolio_api
from twinflow.scheduling import (
    BaselineSolver,
    CompiledProblem,
    Schedule,
    SolveRequest,
    apply_overrides,
    solve,
    verify,
)

T = TypeVar("T")
Data = dict[str, Any]

MAX_ATTEMPTS = 25
DAYS = 60
DAY = 300.0
"""A plant 'day' of 300 s keeps calendars short while still having breaks and shifts."""

GROUPS = (
    "core",
    "calendars",
    "setups",
    "operators",
    "batches",
    "precedence",
    "state",
    "restart",
    "mixed",
)
BASELINE_GROUPS = tuple(group for group in GROUPS if group != "restart")
"""Groups the TF-002 baseline can solve (it refuses `restart`, TF-003 D-074)."""


class Tf003Unavailable(RuntimeError):
    """The TF-003 portfolio API is not installed; restart plans cannot be solved yet."""


@dataclass(frozen=True)
class GeneratedPlan:
    """A verified plan of a generated request, with what produced it."""

    group: str
    seed: int
    attempt: int
    method: str
    request: SolveRequest
    compiled: CompiledProblem
    schedule: Schedule


# --- seeded choices (the patterns of tests/scheduling/generators.py) -------------------------


def _int(rng: np.random.Generator, low: int, high: int) -> int:
    """Uniform integer in [low, high]."""
    return int(rng.integers(low, high + 1))


def _num(rng: np.random.Generator, low: int, high: int) -> float:
    """Uniform integer in [low, high] as float seconds."""
    return float(_int(rng, low, high))


def _pick(rng: np.random.Generator, items: Sequence[T]) -> T:
    return items[int(rng.integers(len(items)))]


def _chance(rng: np.random.Generator, p: float) -> bool:
    return bool(rng.random() < p)


# --- plain-data shapes --------------------------------------------------------------------------


@dataclass
class _Part:
    """One feature group's shop as plain request data; parts merge into `mixed`."""

    jobs: list[Data] = field(default_factory=list)
    machines: list[Data] = field(default_factory=list)
    work_centers: list[Data] = field(default_factory=list)
    calendars: list[Data] = field(default_factory=list)
    pools: list[Data] = field(default_factory=list)
    setup_functions: list[Data] = field(default_factory=list)
    overrides: list[Data] = field(default_factory=list)

    def merge(self, other: _Part) -> None:
        """Append every list of `other` (ids are namespaced by prefix, so none clash)."""
        for name in vars(self):
            getattr(self, name).extend(getattr(other, name))


def _option(machine_id: str, fixed: float, unit: float = 0.0, center: str | None = None) -> Data:
    option: Data = {
        "machine_id": machine_id,
        "fixed_seconds": fixed,
        "unit_seconds": unit,
        "preference_cost": 0.0,
    }
    if center is not None:
        option["work_center_id"] = center
    return option


def _operation(op_id: str, center: str, quantity: float, options: list[Data], **extra: Any) -> Data:
    return {
        "id": op_id,
        "work_center_id": center,
        "quantity": quantity,
        "options": options,
        **extra,
    }


def _chain(operations: list[Data]) -> list[Data]:
    """Link each operation to the previous one end-to-gate (min lag 0)."""
    for pred, succ in zip(operations, operations[1:], strict=False):
        succ.setdefault("predecessors", []).append({"operation_id": pred["id"]})
    return operations


def _calendar(
    calendar_id: str,
    windows: Sequence[tuple[float, float]],
    overtime: dict[tuple[float, float], float],
) -> Data:
    """`windows` repeat every DAY; `overtime` = {(start, end): multiplier} inside a window."""
    intervals = [(k * DAY + a, k * DAY + b) for k in range(DAYS) for a, b in windows]
    priced = [
        {"start": k * DAY + a, "end": k * DAY + b, "cost_multiplier": multiplier}
        for k in range(DAYS)
        for (a, b), multiplier in overtime.items()
    ]
    return {"id": calendar_id, "intervals": intervals, "overtime": priced}


def _override(prefix: str, name: str, kind: str, **fields: Any) -> Data:
    return {
        "id": f"{prefix}{name}",
        "kind": kind,
        "hardness": "hard",
        "fact_ref": "generated",
        "reason": "fidelity test",
        **fields,
    }


# --- core -----------------------------------------------------------------------------------------


def _core_operation(rng: np.random.Generator, p: str, op_id: str, now: float) -> Data:
    """An op of center A (machines A1/A2, substitute B1) or B (B1, substitute A2)."""
    quantity, fixed, unit = _num(rng, 1, 5), _num(rng, 5, 30), _num(rng, 0, 4)
    if _chance(rng, 0.6):
        center, own = f"{p}WA", [f"{p}A1", f"{p}A2"][: _int(rng, 1, 2)]
        substitute = (f"{p}B1", f"{p}WB")
    else:
        center, own = f"{p}WB", [f"{p}B1"]
        substitute = (f"{p}A2", f"{p}WA")
    options = [_option(m, fixed + _num(rng, 0, 5), unit) for m in own]
    if _chance(rng, 0.35):
        options.append(_option(substitute[0], fixed, unit, center=substitute[1]))
    ready = now + _pick(rng, (0.0, 0.0, _num(rng, 10, 60)))
    return _operation(op_id, center, quantity, options, ready_time=ready)


def _core(rng: np.random.Generator, p: str, now: float) -> _Part:
    part = _Part(
        work_centers=[{"id": f"{p}WA"}, {"id": f"{p}WB"}],
        machines=[
            {"id": f"{p}A1", "work_center_id": f"{p}WA"},
            {"id": f"{p}A2", "work_center_id": f"{p}WA"},
            {"id": f"{p}B1", "work_center_id": f"{p}WB"},
        ],
    )
    late = _pick(rng, part.machines)
    late["state"] = {"available_from": now + _num(rng, 20, 60)}
    for j in range(_int(rng, 3, 6)):
        ops = [_core_operation(rng, p, f"{p}J{j}-{k}", now) for k in range(_int(rng, 1, 3))]
        job: Data = {
            "id": f"{p}J{j}",
            "release": now + _pick(rng, (0.0, 0.0, _num(rng, 5, 40))),
            "operations": _chain(ops),
        }
        if _chance(rng, 0.6):
            job["due"] = now + _num(rng, 50, 400)
        part.jobs.append(job)
    return part


# --- calendars ------------------------------------------------------------------------------------


def _calendar_operation(rng: np.random.Generator, p: str, op_id: str) -> Data:
    crossing = _pick(rng, ("forbid", "forbid", "pause", "finish_unattended"))
    span = {"forbid": (10, 60), "pause": (60, 200), "finish_unattended": (40, 150)}[crossing]
    run = _num(rng, *span)
    machines = [str(m) for m in rng.permutation([f"{p}M1", f"{p}M2", f"{p}M3"])[: _int(rng, 1, 2)]]
    options = [_option(m, run) for m in machines]
    return _operation(op_id, f"{p}W", 1.0, options, shift_crossing=crossing)


def _calendars(rng: np.random.Generator, p: str, now: float) -> _Part:
    # C1: [0,100) break [110,200)+[200,240) overtime; C2: [30,150) break [170,260)+[260,280) OT.
    c1 = _calendar(f"{p}C1", ((0, 100), (110, 200), (200, 240)), {(200, 240): 1.5})
    c2 = _calendar(f"{p}C2", ((30, 150), (170, 260), (260, 280)), {(260, 280): 2.0})
    part = _Part(
        work_centers=[{"id": f"{p}W"}],
        calendars=[c1, c2],
        machines=[
            {"id": f"{p}M1", "work_center_id": f"{p}W", "calendar_id": f"{p}C1"},
            {"id": f"{p}M2", "work_center_id": f"{p}W", "calendar_id": f"{p}C1"},
            {"id": f"{p}M3", "work_center_id": f"{p}W", "calendar_id": f"{p}C2"},
        ],
    )
    for j in range(_int(rng, 4, 7)):
        ops = [_calendar_operation(rng, p, f"{p}J{j}-{k}") for k in range(_int(rng, 1, 3))]
        release = now + _num(rng, 0, 400)
        part.jobs.append({"id": f"{p}J{j}", "release": release, "operations": _chain(ops)})
    return part


# --- setups ---------------------------------------------------------------------------------------


def _setup_functions(p: str) -> list[Data]:
    matrix = [
        {"from_family": a, "to_family": b, "seconds": s}
        for (a, b), s in {
            ("A", "B"): 9.0,
            ("B", "A"): 4.0,
            ("A", "A"): 1.0,
            ("B", "B"): 1.5,
        }.items()
    ]
    return [
        {
            "id": f"{p}S_SUM",
            "terms": [
                {"kind": "abs_diff", "param": "w", "seconds_per_unit": 2.0},
                {"kind": "mismatch", "param": "c", "seconds": 6.0},
            ],
            "family_matrix": matrix,
            "default_seconds": 3.0,
        },
        {"id": f"{p}S_FAM", "terms": [], "family_matrix": matrix, "default_seconds": 2.0},
    ]


def _setup_fields(rng: np.random.Generator) -> Data:
    """Full parameters (parameter step), family only (matrix step) or nothing (default)."""
    mode = _pick(rng, ("params", "params", "family", "none"))
    if mode == "params":
        params = {"w": _num(rng, 0, 5), "c": _pick(rng, ("x", "y"))}
        return {"setup_params": params, "setup_family": _pick(rng, ("A", "B"))}
    if mode == "family":
        return {"setup_family": _pick(rng, ("A", "B"))}
    return {}


def _setup_operation(rng: np.random.Generator, p: str, op_id: str, now: float) -> Data:
    machines = [str(m) for m in rng.permutation([f"{p}M1", f"{p}M2", f"{p}M3"])[: _int(rng, 1, 2)]]
    run, unit, quantity = _num(rng, 5, 30), _num(rng, 0, 3), _num(rng, 1, 4)
    extra = _setup_fields(rng)
    if _chance(rng, 0.4):
        extra |= {"setup_detached": True, "ready_time": now + _num(rng, 5, 80)}
    options = [_option(m, run, unit) for m in machines]
    return _operation(op_id, f"{p}W", quantity, options, **extra)


def _setups(rng: np.random.Generator, p: str, now: float) -> _Part:
    w = f"{p}W"
    part = _Part(
        work_centers=[{"id": w, "setup_function_id": f"{p}S_SUM"}],
        setup_functions=_setup_functions(p),
        machines=[
            {
                "id": f"{p}M1",
                "work_center_id": w,
                "setup_function_id": f"{p}S_FAM",
                "state": {"available_from": now + _num(rng, 10, 40), "last_setup_family": "B"},
            },
            {
                "id": f"{p}M2",
                "work_center_id": w,
                "state": {
                    "last_setup_params": {"w": _num(rng, 0, 5), "c": "x"},
                    "last_setup_family": "A",
                },
            },
            {"id": f"{p}M3", "work_center_id": w, "state": {"last_setup_family": "A"}},
        ],
    )
    for j in range(_int(rng, 4, 7)):
        ops = [_setup_operation(rng, p, f"{p}J{j}-{k}", now) for k in range(_int(rng, 1, 3))]
        release = now + _pick(rng, (0.0, _num(rng, 0, 30)))
        part.jobs.append({"id": f"{p}J{j}", "release": release, "operations": _chain(ops)})
    return part


# --- operators ------------------------------------------------------------------------------------

_POOL_PHASES = (("SET", "setup"), ("RUN", "run"), ("BOTH", "both"))


def _operator_operation(rng: np.random.Generator, p: str, op_id: str) -> Data:
    machines = [str(m) for m in rng.permutation([f"{p}M1", f"{p}M2", f"{p}M3"])[: _int(rng, 1, 2)]]
    run = _num(rng, 10, 50)
    chosen = [_POOL_PHASES[int(i)] for i in rng.permutation(3)[: _int(rng, 1, 2)]]
    needs = [{"pool_id": f"{p}P_{pool}", "quantity": 1, "phase": phase} for pool, phase in chosen]
    crossing = "pause" if _chance(rng, 0.3) else "forbid"
    return _operation(
        op_id,
        f"{p}W",
        1.0,
        [_option(m, run) for m in machines],
        operator_needs=needs,
        shift_crossing=crossing,
        setup_params={"c": _pick(rng, ("x", "y"))},
    )


def _operators(rng: np.random.Generator, p: str, now: float) -> _Part:
    w = f"{p}W"
    part = _Part(
        work_centers=[{"id": w, "setup_function_id": f"{p}S"}],
        setup_functions=[
            {
                "id": f"{p}S",
                "terms": [{"kind": "mismatch", "param": "c", "seconds": 8.0}],
                "default_seconds": 4.0,
            }
        ],
        calendars=[
            _calendar(f"{p}MC", ((0, 200),), {}),
            _calendar(f"{p}PC_SET", ((20, 150),), {}),
            _calendar(f"{p}PC_BOTH", ((40, 180),), {}),
        ],
        machines=[
            {"id": f"{p}M1", "work_center_id": w, "calendar_id": f"{p}MC"},
            {"id": f"{p}M2", "work_center_id": w, "calendar_id": f"{p}MC"},
            {"id": f"{p}M3", "work_center_id": w},
        ],
        pools=[
            {"id": f"{p}P_SET", "size": 1, "calendar_id": f"{p}PC_SET"},
            {"id": f"{p}P_RUN", "size": _int(rng, 1, 2)},
            {"id": f"{p}P_BOTH", "size": 1, "calendar_id": f"{p}PC_BOTH"},
        ],
    )
    for j in range(_int(rng, 4, 7)):
        ops = [_operator_operation(rng, p, f"{p}J{j}-{k}") for k in range(_int(rng, 1, 2))]
        release = now + _num(rng, 0, 60)
        part.jobs.append({"id": f"{p}J{j}", "release": release, "operations": _chain(ops)})
    return part


# --- batches --------------------------------------------------------------------------------------

_BATCH_RUN = {"B1": 40.0, "B2": 50.0}
"""Members of one batch must have equal run seconds, so batch options are fixed-only."""


def _batch_job(rng: np.random.Generator, p: str, j: int, now: float) -> Data:
    machines = [m for m in _BATCH_RUN if _chance(rng, 0.7)] or ["B1"]
    batch_op = _operation(
        f"{p}J{j}-1",
        f"{p}WB",
        _num(rng, 1, 2),
        [_option(f"{p}{m}", _BATCH_RUN[m]) for m in machines],
        batch_family=_pick(rng, ("F", "F", "G")),
    )
    ops = [batch_op]
    if _chance(rng, 0.5):
        ops.insert(
            0, _operation(f"{p}J{j}-0", f"{p}WA", 1.0, [_option(f"{p}M1", _num(rng, 5, 20))])
        )
    if _chance(rng, 0.4):
        ops.append(_operation(f"{p}J{j}-2", f"{p}WA", 1.0, [_option(f"{p}M1", _num(rng, 5, 20))]))
    release = now + _pick(rng, (0.0, 0.0, 0.0, 10.0))
    return {"id": f"{p}J{j}", "release": release, "operations": _chain(ops)}


def _batches(rng: np.random.Generator, p: str, now: float) -> _Part:
    return _Part(
        work_centers=[{"id": f"{p}WA"}, {"id": f"{p}WB", "setup_function_id": f"{p}S_B"}],
        setup_functions=[{"id": f"{p}S_B", "terms": [], "default_seconds": 6.0}],
        machines=[
            {"id": f"{p}M1", "work_center_id": f"{p}WA"},
            {"id": f"{p}B1", "work_center_id": f"{p}WB", "batch": {"capacity": 4.0, "unit": "pc"}},
            {"id": f"{p}B2", "work_center_id": f"{p}WB", "batch": {"capacity": 3.0, "unit": "pc"}},
        ],
        jobs=[_batch_job(rng, p, j, now) for j in range(_int(rng, 3, 7))],
    )


# --- precedence -----------------------------------------------------------------------------------


def _lot(rng: np.random.Generator, p: str, op_id: str) -> Data:
    machines = [str(m) for m in rng.permutation([f"{p}M1", f"{p}M2", f"{p}M3"])[: _int(rng, 1, 2)]]
    quantity = _num(rng, 2, 6)
    options = [_option(m, _num(rng, 2, 10), _num(rng, 1, 5)) for m in machines]
    return _operation(op_id, f"{p}W", quantity, options)


def _link(rng: np.random.Generator, pred: Data) -> Data:
    link: Data = {"operation_id": pred["id"]}
    if _chance(rng, 0.4):
        link["min_lag"] = _num(rng, 5, 30)
    if _chance(rng, 0.3):
        link["max_lag"] = link.get("min_lag", 0.0) + _num(rng, 400, 900)
    if _chance(rng, 0.35):
        link["overlap_quantity"] = _num(rng, 1, int(pred["quantity"]) - 1)
    return link


_DAG_SHAPES = (
    ((), (0,)),  # a -> b
    ((), (0,), (1,)),  # chain
    ((), (0,), (0,), (1, 2)),  # diamond: fork at a, join at d
    ((), (), (0, 1)),  # two lots join
)


def _precedence_job(rng: np.random.Generator, p: str, j: int, now: float) -> Data:
    shape = _pick(rng, _DAG_SHAPES)
    ops = [_lot(rng, p, f"{p}J{j}-{k}") for k in range(len(shape))]
    for op, preds in zip(ops, shape, strict=True):
        if preds:
            op["predecessors"] = [_link(rng, ops[i]) for i in preds]
    if _chance(rng, 0.4):
        target = _pick(rng, ops)
        earliest = now + _num(rng, 0, 80)
        target["pin"] = _pick(
            rng,
            (
                {"earliest_start": earliest, "latest_start": earliest + 2000.0},
                {"machine_id": target["options"][0]["machine_id"]},
            ),
        )
    return {"id": f"{p}J{j}", "release": now + _num(rng, 0, 30), "operations": ops}


def _precedence(rng: np.random.Generator, p: str, now: float) -> _Part:
    w = f"{p}W"
    return _Part(
        work_centers=[{"id": w}],
        machines=[{"id": f"{p}M{m}", "work_center_id": w} for m in (1, 2, 3)],
        jobs=[_precedence_job(rng, p, j, now) for j in range(_int(rng, 3, 5))],
    )


# --- state ----------------------------------------------------------------------------------------


def _simple(rng: np.random.Generator, p: str, op_id: str, machines: Sequence[str]) -> Data:
    options = [_option(f"{p}{m}", _num(rng, 5, 30)) for m in machines]
    return _operation(op_id, f"{p}W", 1.0, options)


def _state_jobs(rng: np.random.Generator, p: str, now: float) -> list[Data]:
    def job(name: str, ops: list[Data], **extra: Any) -> Data:
        return {"id": f"{p}{name}", "operations": _chain(ops), "release": now, **extra}

    running = _simple(rng, p, f"{p}RUN-0", ("M1", "M3"))
    running["status"] = {
        "state": "in_progress",
        "machine_id": f"{p}M1",
        "remaining_seconds": _num(rng, 10, 60),
    }
    paused = _operation(
        f"{p}PAU-0",
        f"{p}W",
        1.0,
        [_option(f"{p}M2", _num(rng, 20, 80))],
        operator_needs=[{"pool_id": f"{p}CREW", "quantity": 1, "phase": "both"}],
    )
    done_first = _simple(rng, p, f"{p}DON-0", ("M3",))
    done_first["status"] = {"state": "done"}
    finished = _simple(rng, p, f"{p}FIN-0", ("M4",))
    late_material = _simple(rng, p, f"{p}MAT-0", ("M3", "M4"))
    late_material["ready_time"] = now + _num(rng, 30, 120)
    return [
        job("RUN", [running, _simple(rng, p, f"{p}RUN-1", ("M2", "M3"))]),
        job("PAU", [paused, _simple(rng, p, f"{p}PAU-1", ("M4",))]),
        job("HST", [_simple(rng, p, f"{p}HST-0", ("M1",))], status="held"),
        job("HOV", [_simple(rng, p, f"{p}HOV-0", ("M2",))]),
        job("HUN", [_simple(rng, p, f"{p}HUN-0", ("M3", "M4"))]),
        job("DON", [done_first, _simple(rng, p, f"{p}DON-1", ("M1", "M4"))]),
        job("FIN", [finished], status="done"),
        job(
            "SPL",
            [_simple(rng, p, f"{p}SPL-0", ("M1", "M2")), _simple(rng, p, f"{p}SPL-1", ("M4",))],
        ),
        job("MAT", [late_material]),
    ]


def _state(rng: np.random.Generator, p: str, now: float) -> _Part:
    w = f"{p}W"
    jobs = _state_jobs(rng, p, now)
    if _chance(rng, 0.5):  # an external link into the split job fans out to its children
        jobs[-1]["operations"][0]["predecessors"] = [{"operation_id": f"{p}SPL-1"}]
    shift_open = now % DAY + _num(rng, 30, 80)  # the crew is off shift at now
    return _Part(
        work_centers=[{"id": w}],
        machines=[{"id": f"{p}M{m}", "work_center_id": w} for m in (1, 2, 3, 4)],
        calendars=[_calendar(f"{p}CREWCAL", ((shift_open, shift_open + 150.0),), {})],
        pools=[{"id": f"{p}CREW", "size": 1, "calendar_id": f"{p}CREWCAL"}],
        jobs=jobs,
        overrides=[
            _override(p, "paused-lock", "lock", operation_id=f"{p}PAU-0", machine_id=f"{p}M2"),
            _override(p, "hold-out", "hold", job_id=f"{p}HOV", until=None),
            _override(p, "hold-until", "hold", job_id=f"{p}HUN", until=now + _num(rng, 40, 150)),
            _override(
                p,
                "split",
                "split",
                job_id=f"{p}SPL",
                fractions=_pick(rng, ((0.5, 0.5), (0.25, 0.75))),
            ),
        ],
    )


# --- restart (TF-003) -----------------------------------------------------------------------------


def _restart(rng: np.random.Generator, p: str, now: float) -> _Part:
    w = f"{p}W"
    restart = {"idle_threshold_seconds": 30.0, "warmup_seconds": _num(rng, 5, 15)}
    part = _Part(
        work_centers=[{"id": w, "setup_function_id": f"{p}S"}],
        setup_functions=[{"id": f"{p}S", "terms": [], "default_seconds": 3.0}],
        machines=[
            {"id": f"{p}R1", "work_center_id": w, "restart": restart},
            {"id": f"{p}R2", "work_center_id": w, "restart": restart},
        ],
    )
    for j in range(_int(rng, 3, 6)):
        ops = [_simple(rng, p, f"{p}J{j}-{k}", ("R1", "R2")) for k in range(_int(rng, 1, 2))]
        release = now + _num(rng, 0, 300)  # spread releases so machines idle past threshold
        part.jobs.append({"id": f"{p}J{j}", "release": release, "operations": _chain(ops)})
    return part


# --- mixed ----------------------------------------------------------------------------------------

_MIXED_PARTS = ("core", "calendars", "setups", "operators", "batches", "precedence", "state")


def _mixed(rng: np.random.Generator, p: str, now: float) -> _Part:
    part = _Part()
    for name in _MIXED_PARTS:
        part.merge(_BUILDERS[name](rng, f"{p}{name[:3]}_", now))
    _cross_links(rng, part)
    return part


def _cross_links(rng: np.random.Generator, part: _Part) -> None:
    """Two links between parts so the parts interact (a core op feeds a precedence lot ...)."""
    open_jobs = [
        job
        for job in part.jobs
        if job.get("status", "open") == "open" and not job["id"].startswith(("sta_",))
    ]
    for _ in range(2):
        first, second = sorted(int(i) for i in rng.permutation(len(open_jobs))[:2])
        # always from the earlier job to the later one, so the links never close a cycle
        pred, succ = open_jobs[first]["operations"][-1], open_jobs[second]["operations"][0]
        succ.setdefault("predecessors", []).append({"operation_id": pred["id"]})


_BUILDERS: dict[str, Callable[[np.random.Generator, str, float], _Part]] = {
    "core": _core,
    "calendars": _calendars,
    "setups": _setups,
    "operators": _operators,
    "batches": _batches,
    "precedence": _precedence,
    "state": _state,
    "restart": _restart,
    "mixed": _mixed,
}
_NOW = {"core": 10.0, "state": 100.0, "mixed": 100.0}
"""`now` per group (0 elsewhere); state and mixed need a non-zero now for in-progress work."""


# --- requests, solving and verification -----------------------------------------------------------


def build_request(group: str, seed: int, attempt: int = 0) -> SolveRequest:
    """The request of (group, seed, attempt); the same inputs always give the same request."""
    rng = np.random.default_rng((GROUPS.index(group), seed, attempt))
    now = _NOW.get(group, 0.0)
    part = _BUILDERS[group](rng, "", now)
    problem: Data = {
        "id": f"{group}-{seed}-{attempt}",
        "now": now,
        "jobs": part.jobs,
        "machines": part.machines,
        "work_centers": part.work_centers,
        "calendars": part.calendars,
        "operator_pools": part.pools,
        "setup_functions": part.setup_functions,
    }
    data = {"schema_version": "2.0", "problem": problem, "overrides": part.overrides}
    return SolveRequest.model_validate(data)


def reverified(request: SolveRequest, compiled: CompiledProblem, schedule: Schedule) -> bool:
    """The simulation service's re-verification: feasibility only (COMP-139, D-087)."""
    unvalued = schedule.model_copy(update={"primary_value": None})
    return verify(compiled, request.overrides, unvalued, request.previous_plan).valid


def _baseline_plan(group: str, seed: int, attempt: int) -> GeneratedPlan | None:
    request = build_request(group, seed, attempt)
    result = solve(request, solver=BaselineSolver())
    compiled = apply_overrides(request)
    if not (result.verified and reverified(request, compiled, result.schedule)):
        return None
    return GeneratedPlan(group, seed, attempt, "baseline", request, compiled, result.schedule)


def _generate(group: str, seed: int) -> GeneratedPlan:
    for attempt in range(MAX_ATTEMPTS):
        plan = _baseline_plan(group, seed, attempt)
        if plan is not None:
            return plan
    raise AssertionError(f"{group} seed {seed}: no verified baseline plan in {MAX_ATTEMPTS} tries")


def generate_core(seed: int) -> GeneratedPlan:
    """Core group: alternatives, substitute, precedence, release, ready time, available_from."""
    return _generate("core", seed)


def generate_calendars(seed: int) -> GeneratedPlan:
    """Calendars group: breaks, overtime, forbid / pause / finish_unattended."""
    return _generate("calendars", seed)


def generate_setups(seed: int) -> GeneratedPlan:
    """Setups group: abs_diff, mismatch, family matrix, detached, machine state."""
    return _generate("setups", seed)


def generate_operators(seed: int) -> GeneratedPlan:
    """Operators group: pools by phase, pool calendars narrower than machine calendars."""
    return _generate("operators", seed)


def generate_batches(seed: int) -> GeneratedPlan:
    """Batches group: batch machines and batch-family operations."""
    return _generate("batches", seed)


def generate_precedence(seed: int) -> GeneratedPlan:
    """Precedence group: min and max lags, transfer-lot overlap, pins, a lot DAG."""
    return _generate("precedence", seed)


def generate_state(seed: int) -> GeneratedPlan:
    """State group: in-progress, paused, held, hold until, done, split, ready time."""
    return _generate("state", seed)


def generate_mixed(seed: int) -> GeneratedPlan:
    """Mixed group: every baseline group's part in one problem, with cross-part links."""
    return _generate("mixed", seed)


def generate_restart(seed: int) -> GeneratedPlan:
    """Restart group, solved by the first verified TF-003 portfolio candidate.

    Raises `Tf003Unavailable` when the TF-003 portfolio API is not installed (the baseline
    refuses `restart`).
    """
    for attempt in range(MAX_ATTEMPTS):
        request = build_request("restart", seed, attempt)
        plans = tf003_plans_for(request, "restart", seed, attempt)
        if plans:
            return plans[0]
    raise AssertionError(f"restart seed {seed}: no verified TF-003 plan in {MAX_ATTEMPTS} tries")


GENERATORS: dict[str, Callable[[int], GeneratedPlan]] = {
    "core": generate_core,
    "calendars": generate_calendars,
    "setups": generate_setups,
    "operators": generate_operators,
    "batches": generate_batches,
    "precedence": generate_precedence,
    "state": generate_state,
    "restart": generate_restart,
    "mixed": generate_mixed,
}


def tf003_plans_for(
    request: SolveRequest, group: str, seed: int, attempt: int
) -> tuple[GeneratedPlan, ...]:
    """One plan per verified TF-003 portfolio candidate (method = its pipeline).

    Raises `Tf003Unavailable` while the TF-003 portfolio API is absent. Candidates are
    re-verified here exactly as the simulation service does; an unverified one is skipped.
    """
    api = load_portfolio_api()
    if api is None:
        raise Tf003Unavailable("TF-003 portfolio api not installed yet")
    compiled = apply_overrides(request)
    result = api.solve_portfolio(request, seed=seed)
    return tuple(
        GeneratedPlan(group, seed, attempt, "+".join(c.pipeline), request, compiled, c.schedule)
        for c in result.candidates
        if c.verified and reverified(request, compiled, c.schedule)
    )


def tf003_plans(plan: GeneratedPlan) -> tuple[GeneratedPlan, ...]:
    """TF-003 plans of the same request as a baseline `plan` (raises when TF-003 is absent)."""
    return tf003_plans_for(plan.request, plan.group, plan.seed, plan.attempt)
