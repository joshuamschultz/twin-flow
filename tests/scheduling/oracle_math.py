"""Independent setup and objective arithmetic for the test oracle (COMP-012, REQ-079).

Written from the TF-002 SDD text (Data Model, "Objective formulas", DEV-4) and D-064,
not from `setups.py` or `objectives.py`. The oracle checks solvers against known optima;
if it shared the production arithmetic, a bug in that arithmetic would pass every test.
`test_oracle.py` cross-checks both sides over generated problems instead.

Setup seconds before op `n` on machine i (D-064, DEV-4). The function is the machine's
`setup_function_id`, else its work center's, else none (setup 0). "Before" is the previous
op on the machine, or the machine state (`last_setup_params`, `last_setup_family`) for
the first op. With terms, when both sides carry every term's parameter (a text value
counts as missing for `abs_diff`), the terms are combined by sum or max. Otherwise the
family matrix entry (both families set, entry present; the first matching entry, since
the model allows repeats), otherwise `default_seconds`.

Objective scope: only assignments of ops in the compiled problem whose job is not held
count. A job counts when one of its ops is assigned; C_j is its latest end. Empty sums,
maxima and ratios give 0.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    Job,
    MachineState,
    ObjectiveKind,
    Operation,
    PreviousPlan,
    Problem,
    Schedule,
    SetupFunction,
    SetupValue,
)

MAXIMIZED = frozenset({ObjectiveKind.ROBUSTNESS})
"""Objectives with sense max; every other objective is minimized."""

_HOUR = 3600.0

Params = Mapping[str, SetupValue]


# --- setups ------------------------------------------------------------------------------------


def _numeric(value: SetupValue) -> bool:
    return not isinstance(value, str)


def _terms_seconds(function: SetupFunction, before: Params, after: Params) -> float | None:
    """The combined term seconds, or None when any term lacks a usable value."""
    parts: list[float] = []
    for term in function.terms:
        if term.param not in before or term.param not in after:
            return None
        old, new = before[term.param], after[term.param]
        if term.kind == "mismatch":
            parts.append(0.0 if old == new else term.seconds)
            continue
        if not (_numeric(old) and _numeric(new)):
            return None
        parts.append(term.seconds_per_unit * abs(float(new) - float(old)))
    return max(parts) if function.combine == "max" else math.fsum(parts)


def _matrix_seconds(function: SetupFunction, before: str | None, after: str | None) -> float | None:
    if before is None or after is None:
        return None
    matches = [
        e.seconds for e in function.family_matrix if (e.from_family, e.to_family) == (before, after)
    ]
    return matches[0] if matches else None


def _function_seconds(
    function: SetupFunction, before: tuple[Params, str | None], after: Operation
) -> float:
    by_terms = _terms_seconds(function, before[0], after.setup_params) if function.terms else None
    if by_terms is not None:
        return by_terms
    by_matrix = _matrix_seconds(function, before[1], after.setup_family)
    return function.default_seconds if by_matrix is None else by_matrix


class SetupCalculator:
    """Setup seconds per (machine, previous op or None, next op) for one problem; no cache."""

    def __init__(self, problem: Problem) -> None:
        functions = {f.id: f for f in problem.setup_functions}
        center_function = {c.id: c.setup_function_id for c in problem.work_centers}
        self._function: dict[str, SetupFunction | None] = {}
        self._state: dict[str, MachineState] = {}
        for machine in problem.machines:
            chosen = machine.setup_function_id or center_function.get(machine.work_center_id)
            self._function[machine.id] = functions[chosen] if chosen else None
            self._state[machine.id] = machine.state
        self._op = {op.id: op for job in problem.jobs for op in job.operations}

    def seconds(self, machine_id: str, prev_id: str | None, next_id: str) -> float:
        """Setup seconds before `next_id`; `prev_id` None means the machine state at `now`."""
        function = self._function[machine_id]
        if function is None:
            return 0.0
        if prev_id is None:
            state = self._state[machine_id]
            before = (state.last_setup_params, state.last_setup_family)
        else:
            prev = self._op[prev_id]
            before = (prev.setup_params, prev.setup_family)
        return _function_seconds(function, before, self._op[next_id])


# --- objectives --------------------------------------------------------------------------------


def objective_values(
    compiled: CompiledProblem, schedule: Schedule, previous_plan: PreviousPlan | None = None
) -> dict[ObjectiveKind, float]:
    """All thirteen objective values of `schedule` by the SDD "Objective formulas"."""
    held = set(compiled.held_job_ids)
    job_of = {
        op.id: job for job in compiled.problem.jobs if job.id not in held for op in job.operations
    }
    kept = [a for a in schedule.assignments if a.operation_id in job_of]
    finish: dict[str, float] = {}
    weight: dict[str, float] = {}
    due: dict[str, float] = {}
    for a in kept:
        job = job_of[a.operation_id]
        finish[job.id] = max(finish.get(job.id, a.end), a.end)
        weight[job.id] = _weight(job, compiled.profile.class_weights)
        if job.due is not None:
            due[job.id] = job.due
    now = compiled.problem.now
    return {
        ObjectiveKind.MAKESPAN: max(finish.values()) - now if finish else 0.0,
        ObjectiveKind.WEIGHTED_COMPLETION: math.fsum(weight[j] * c for j, c in finish.items()),
        **_due_date_values(finish, weight, due, now),
        **_assignment_values(compiled.problem, kept),
        ObjectiveKind.CHURN: _churn(compiled, kept, previous_plan),
    }


def _weight(job: Job, class_weights: Mapping[int, float]) -> float:
    return (
        job.weight_override if job.weight_override is not None else class_weights[job.weight_class]
    )


def _due_date_values(
    finish: Mapping[str, float], weight: Mapping[str, float], due: Mapping[str, float], now: float
) -> dict[ObjectiveKind, float]:
    late = {j: finish[j] - d for j, d in due.items()}
    tardy = [j for j, lateness in late.items() if lateness > 0]
    robust_den = math.fsum(weight[j] * (d - now) for j, d in due.items())
    robust_num = math.fsum(weight[j] * (d - finish[j]) for j, d in due.items())
    return {
        ObjectiveKind.MAX_LATENESS: max(late.values()) if late else 0.0,
        ObjectiveKind.TARDY_COUNT: float(len(tardy)),
        ObjectiveKind.WEIGHTED_TARDY_COUNT: math.fsum(weight[j] for j in tardy),
        ObjectiveKind.TOTAL_TARDINESS: math.fsum(late[j] for j in tardy),
        ObjectiveKind.WEIGHTED_TARDINESS: math.fsum(weight[j] * late[j] for j in tardy),
        ObjectiveKind.EARLINESS_TARDINESS: math.fsum(abs(x) for x in late.values()),
        ObjectiveKind.ROBUSTNESS: robust_num / robust_den if robust_den > 0 else 0.0,
    }


def _assignment_values(problem: Problem, kept: list[Assignment]) -> dict[ObjectiveKind, float]:
    machine = {m.id: m for m in problem.machines}
    price = {
        (op.id, o.machine_id): o.preference_cost
        for job in problem.jobs
        for op in job.operations
        for o in op.options
    }
    run_cost = math.fsum((a.end - a.setup_end) * machine[a.machine_id].hour_cost for a in kept)
    setup_cost = math.fsum(
        (a.setup_end - a.start) * machine[a.machine_id].setup_hour_cost for a in kept
    )
    return {
        ObjectiveKind.TOTAL_SETUP: math.fsum(a.setup_end - a.start for a in kept),
        ObjectiveKind.COST: (run_cost + setup_cost) / _HOUR,
        ObjectiveKind.SOFT_PENALTY: math.fsum(price[a.operation_id, a.machine_id] for a in kept),
    }


def _churn(
    compiled: CompiledProblem, kept: list[Assignment], previous_plan: PreviousPlan | None
) -> float:
    """Previous-plan ops now on another machine or moved more than the frozen tolerance."""
    if previous_plan is None:
        return 0.0
    zone = compiled.profile.frozen_zone
    slack = zone.tolerance if zone is not None else 0.0
    placed = {a.operation_id: a for a in kept}
    count = 0
    for before in previous_plan.assignments:
        after = placed.get(before.operation_id)
        if after is not None and (
            after.machine_id != before.machine_id or abs(after.start - before.start) > slack
        ):
            count += 1
    return float(count)
