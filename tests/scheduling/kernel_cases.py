"""Shared problem builders for the kernel tests (T-120..T-123, COMP-003..COMP-006).

Import as `from kernel_cases import ...` (pytest's `prepend` mode puts this directory on
`sys.path`). Nothing here imports the kernel, so a missing kernel module fails only the
tests that use it.

- `load_request`, `compile_request`: Pinedo fixtures and generated requests, compiled.
- `machine_sequences`, `machine_choice`: a schedule's per-machine start order (the
  verifier's order: start, end, op id) and its op -> machine map, i.e. decoder input.
- `pinedo_16_4_1`: Example 16.4.1 built inline from the PLAN T-126 text, plus the book's
  sequences, reconstructed by brute force over every stage-1 order and stage-2 split
  (the stage-3 order is fixed by the book's completion times): stage-2 jobs {1,4,6} on one
  machine and {2,5,3} on the other is the only split that reproduces C = 25, 43, 93, 62,
  75, 81.5; four stage-1 orders do, and 1,2,4,5,6,3 is the bottleneck start order (the
  five-phase rule, p.446), which is also the bottleneck completion order used at stage 3.
- `without_features`: a generated request with feature families removed, so a property
  that holds only without them can still run on 200 seeds.
- `random_job_shop`: a small seeded job shop with fixed machine choice and acyclic
  per-machine sequences, for the graph tests.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from twinflow.scheduling.model import (
    CompiledProblem,
    Schedule,
    SolveRequest,
)
from twinflow.scheduling.overrides import apply_overrides

FIXTURES = Path(__file__).parent / "fixtures"

OPTIMAL_7_1_1: dict[str, tuple[str, ...]] = {
    "M1": ("1-1", "2-2", "3-1"),
    "M2": ("2-1", "1-2", "3-2"),
    "M3": ("1-3", "2-4"),
    "M4": ("2-3", "3-3"),
}
"""One makespan-28 sequence set of 7.1.1; `test_kernel_decode` re-derives it with the oracle."""

RUNS_7_1_1 = {
    "1-1": 10.0, "1-2": 8.0, "1-3": 4.0,
    "2-1": 8.0, "2-2": 3.0, "2-3": 5.0, "2-4": 6.0,
    "3-1": 4.0, "3-2": 7.0, "3-3": 3.0,
}  # fmt: skip

BOOK_16_4_1_SEQUENCES: dict[str, tuple[str, ...]] = {
    "M1": ("1-1", "2-1", "4-1", "5-1", "6-1", "3-1"),
    "M2": ("1-2", "4-2", "6-2"),
    "M3": ("2-2", "5-2", "3-2"),
    "M4": ("1-3", "2-3", "4-3", "5-3", "6-3", "3-3"),
}
BOOK_16_4_1_COMPLETIONS = {"1": 25.0, "2": 43.0, "3": 93.0, "4": 62.0, "5": 75.0, "6": 81.5}
BOOK_16_4_1_WEIGHTED_TARDINESS = 163.5


def load_request(name: str) -> SolveRequest:
    """A fixture request by file stem, e.g. `pinedo_7_1_1`."""
    data = json.loads((FIXTURES / f"{name}.json").read_text())
    return SolveRequest.model_validate(data)


def compile_request(request: SolveRequest) -> CompiledProblem:
    return apply_overrides(request)


def machine_sequences(schedule: Schedule) -> dict[str, list[str]]:
    """Per-machine op ids in the verifier's order: (start, end, op id)."""
    ordered = sorted(schedule.assignments, key=lambda a: (a.start, a.end, a.operation_id))
    sequences: dict[str, list[str]] = defaultdict(list)
    for a in ordered:
        sequences[a.machine_id].append(a.operation_id)
    return dict(sequences)


def machine_choice(schedule: Schedule) -> dict[str, str]:
    return {a.operation_id: a.machine_id for a in schedule.assignments}


def only_choice(request: SolveRequest) -> dict[str, str]:
    """Op -> its single option machine (fixtures with one option per op)."""
    return {
        op.id: op.options[0].machine_id for job in request.problem.jobs for op in job.operations
    }


def request_from(
    problem: dict[str, Any],
    *,
    profile: dict[str, Any] | None = None,
    overrides: Sequence[dict[str, Any]] = (),
    previous_plan: dict[str, Any] | None = None,
) -> SolveRequest:
    data: dict[str, Any] = {"schema_version": "2.0", "problem": problem}
    if profile is not None:
        data["profile"] = profile
    if overrides:
        data["overrides"] = list(overrides)
    if previous_plan is not None:
        data["previous_plan"] = previous_plan
    return SolveRequest.model_validate(data)


def op(
    op_id: str,
    machines: Mapping[str, float],
    *,
    center: str = "WC",
    after: Iterable[str | dict[str, Any]] = (),
    **fields: Any,
) -> dict[str, Any]:
    """An operation dict: `machines` maps machine id -> fixed seconds (unit 0, pref 0)."""
    links = [{"operation_id": link} if isinstance(link, str) else link for link in after]
    data: dict[str, Any] = {
        "id": op_id,
        "work_center_id": center,
        "quantity": 1.0,
        "options": [
            {"machine_id": m, "fixed_seconds": s, "unit_seconds": 0.0, "preference_cost": 0.0}
            for m, s in machines.items()
        ],
        "predecessors": links,
    }
    return data | fields


def machine(machine_id: str, *, center: str = "WC", **fields: Any) -> dict[str, Any]:
    return {"id": machine_id, "work_center_id": center} | fields


# --- Pinedo Example 16.4.1 ------------------------------------------------------------------

_P = (16.0, 24.0, 20.0, 32.0, 28.0, 22.0)
_RELEASE = (1.0, 5.0, 9.0, 7.0, 15.0, 6.0)
_DUE = (30.0, 35.0, 60.0, 71.0, 27.0, 63.0)
_WEIGHT = (2.0, 2.0, 1.0, 2.0, 2.0, 1.0)
_A_STAGE = ((4, 2, 3, 1, 4, 2), (3, 4, 1, 3, 1, 3), (2, 2, 4, 1, 2, 3))
_STAGES = (("S1", {"M1": 4.0}), ("S2", {"M2": 1.0, "M3": 1.0}), ("S3", {"M4": 4.0}))


def _stage_op(job: int, stage: int) -> dict[str, Any]:
    center, speeds = _STAGES[stage]
    prev = [f"{job + 1}-{stage}"] if stage > 0 else []
    return op(
        f"{job + 1}-{stage + 1}",
        {m: _P[job] / v for m, v in speeds.items()},
        center=center,
        after=prev,
        setup_params={"a": float(_A_STAGE[stage][job])},
        setup_detached=True,
    )


def pinedo_16_4_1() -> SolveRequest:
    """Pinedo Example 16.4.1 (p.443-447) per PLAN T-126, weighted tardiness objective."""
    jobs = [
        {
            "id": str(j + 1),
            "release": _RELEASE[j],
            "due": _DUE[j],
            "weight_override": _WEIGHT[j],
            "operations": [_stage_op(j, s) for s in range(3)],
        }
        for j in range(6)
    ]
    machines = [
        machine(m, center=center, setup_function_id=f"S-{m}")
        for center, speeds in _STAGES
        for m in speeds
    ]
    setup_functions = [
        {"id": f"S-{m}", "terms": [{"kind": "abs_diff", "param": "a", "seconds_per_unit": 1.0}]}
        for _, speeds in _STAGES
        for m in speeds
    ]
    problem = {
        "id": "pinedo-16.4.1",
        "jobs": jobs,
        "machines": machines,
        "work_centers": [{"id": center} for center, _ in _STAGES],
        "setup_functions": setup_functions,
    }
    profile = {"objective": {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}}
    return request_from(problem, profile=profile)


# --- feature stripping ----------------------------------------------------------------------

STRIPPABLE = frozenset(
    {"calendars", "pause", "operators", "batching", "restart", "max_lags", "frozen_zone"}
)


def without_features(request: SolveRequest, drop: Iterable[str]) -> SolveRequest:
    """`request` with the named feature families removed (names from `STRIPPABLE`).

    calendars: no machine or pool calendar; pause: every op forbid; operators: no needs;
    batching: no batch family and no batch machine; restart: no rule; max_lags: no link
    max_lag; frozen_zone: no frozen zone (its compiled pins carry a latest_start).
    """
    names = set(drop)
    unknown = names - STRIPPABLE
    if unknown:
        raise ValueError(f"cannot strip {sorted(unknown)}")
    data = request.model_dump()
    problem = data["problem"]
    for m in problem["machines"]:
        if "calendars" in names:
            m["calendar_id"] = None
        if "batching" in names:
            m["batch"] = None
        if "restart" in names:
            m["restart"] = None
    if "calendars" in names:
        for pool in problem["operator_pools"]:
            pool["calendar_id"] = None
    for job in problem["jobs"]:
        for operation in job["operations"]:
            _strip_operation(operation, names)
    if "frozen_zone" in names:
        data["profile"]["frozen_zone"] = None
    return SolveRequest.model_validate(data)


def _strip_operation(operation: dict[str, Any], names: set[str]) -> None:
    if "pause" in names:
        operation["shift_crossing"] = "forbid"
    if "operators" in names:
        operation["operator_needs"] = ()
    if "batching" in names:
        operation["batch_family"] = None
    if "max_lags" in names:
        for link in operation["predecessors"]:
            link["max_lag"] = None


# --- random job shops for the graph tests ---------------------------------------------------


def random_job_shop(
    seed: int, *, setups: bool = False
) -> tuple[SolveRequest, dict[str, str], dict[str, list[str]]]:
    """A seeded job shop: request, machine choice and acyclic machine sequences.

    3-5 jobs of 2-4 ops on 3 machines, integer runs 1-9, job links with min_lag 0-3. With
    `setups`, an abs_diff setup on param `w` (1 s per unit), random `setup_detached`, a
    machine state with a last `w`, and ops with two options (the choice fixes one).
    Sequences follow one random interleaving of the job chains, so they are acyclic.
    """
    rng = np.random.default_rng((seed, 123))
    machine_ids = ["M0", "M1", "M2"]
    jobs: list[dict[str, Any]] = []
    choice: dict[str, str] = {}
    chains: list[list[str]] = []
    for j in range(int(rng.integers(3, 6))):
        ops: list[dict[str, Any]] = []
        for k in range(int(rng.integers(2, 5))):
            op_id = f"J{j}-{k}"
            options = [str(m) for m in rng.permutation(machine_ids)[: 2 if setups else 1]]
            fields: dict[str, Any] = {}
            if setups:
                fields = {
                    "setup_params": {"w": float(rng.integers(0, 6))},
                    "setup_detached": bool(rng.random() < 0.4),
                }
            after = []
            if ops:
                after = [{"operation_id": ops[-1]["id"], "min_lag": float(rng.integers(0, 4))}]
            runs = {m: float(rng.integers(1, 10)) for m in options}
            ops.append(op(op_id, runs, after=after, **fields))
            choice[op_id] = options[0]
        jobs.append({"id": f"J{j}", "operations": ops})
        chains.append([o["id"] for o in ops])
    machines = [machine(m) for m in machine_ids]
    problem: dict[str, Any] = {
        "id": f"shop-{seed}",
        "jobs": jobs,
        "machines": machines,
        "work_centers": [{"id": "WC"}],
    }
    if setups:
        for m in machines:
            m["state"] = {"last_setup_params": {"w": float(rng.integers(0, 6))}}
        problem["work_centers"] = [{"id": "WC", "setup_function_id": "S"}]
        problem["setup_functions"] = [
            {"id": "S", "terms": [{"kind": "abs_diff", "param": "w", "seconds_per_unit": 1.0}]}
        ]
    sequences: dict[str, list[str]] = {m: [] for m in machine_ids}
    for op_id in _interleave(rng, chains):
        sequences[choice[op_id]].append(op_id)
    return request_from(problem), choice, sequences


def _interleave(rng: np.random.Generator, chains: list[list[str]]) -> list[str]:
    """A random topological order of independent chains."""
    positions = [0] * len(chains)
    order: list[str] = []
    while len(order) < sum(map(len, chains)):
        open_chains = [i for i, chain in enumerate(chains) if positions[i] < len(chain)]
        i = open_chains[int(rng.integers(len(open_chains)))]
        order.append(chains[i][positions[i]])
        positions[i] += 1
    return order
