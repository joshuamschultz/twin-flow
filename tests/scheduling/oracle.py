"""Brute-force test oracle for small scheduling problems (COMP-012, REQ-078, REQ-079).

`semi_active` places every operation as early as possible for fixed per-machine
sequences; `brute_force` enumerates every option choice and every per-machine sequence
and keeps the best schedule by `oracle_math.objective_values`. The oracle is deliberately small:
it supports releases, ready times, machine state, sequence-dependent setups (attached
and detached, D-063), restart warm-ups (D-074) and end-to-gate precedence with `min_lag`.
Problems using calendars, operators, batching, pins, shop-floor status, held jobs,
transfer-lot overlap or `max_lag` raise ValueError, so a test can never trust an oracle
that ignored a constraint.
Setup seconds and objective values come from `oracle_math`, an independent re-implementation
of the SDD text: the oracle never imports `setups`, `objectives` or `verifier`.

Import it as `from oracle import ...`: pytest's default `prepend` import mode puts
`tests/scheduling/` on `sys.path` for the test modules in that directory.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator, Mapping, Sequence

from oracle_math import MAXIMIZED, SetupCalculator, objective_values

from twinflow.scheduling.model import (
    Assignment,
    CompiledProblem,
    ObjectiveKind,
    Operation,
    Problem,
    Schedule,
    SchedulingProfile,
)

MAX_COMBINATIONS = 1_000_000
"""Largest number of (option choice, machine sequences) combinations `brute_force` tries."""

Sequences = Mapping[str, Sequence[str]]


class SequenceDeadlock(ValueError):
    """The machine sequences contradict precedence, so no schedule exists for them."""


# --- input checks ------------------------------------------------------------------------------


def _unsupported_features(problem: Problem) -> list[str]:
    found = [f"machine {m.id} calendar" for m in problem.machines if m.calendar_id]
    found += [f"machine {m.id} batch" for m in problem.machines if m.batch]
    found += [f"job {j.id} status {j.status}" for j in problem.jobs if j.status != "open"]
    for op in _operations(problem):
        found += _unsupported_operation_features(op)
    return found


def _unsupported_operation_features(op: Operation) -> list[str]:
    checks = {
        "operator_needs": bool(op.operator_needs),
        "batch_family": op.batch_family is not None,
        "pin": op.pin is not None,
        "status": op.status.state != "pending",
        "max_lag": any(p.max_lag is not None for p in op.predecessors),
        "overlap_quantity": any(p.overlap_quantity is not None for p in op.predecessors),
    }
    return [f"operation {op.id} {name}" for name, used in checks.items() if used]


def _check_supported(problem: Problem) -> None:
    found = _unsupported_features(problem)
    if found:
        raise ValueError(f"oracle does not support: {', '.join(found)}")


def _check_sequences(problem: Problem, sequences: Sequences) -> None:
    """Each operation exactly once, on a known machine that is one of its options."""
    machine_ids = {m.id for m in problem.machines}
    unknown_machines = set(sequences) - machine_ids
    if unknown_machines:
        raise ValueError(f"sequence names unknown machines {sorted(unknown_machines)}")
    listed = [op_id for seq in sequences.values() for op_id in seq]
    expected = sorted(op.id for op in _operations(problem))
    if sorted(listed) != expected:
        raise ValueError("the sequences must list every operation exactly once")
    options = {op.id: {o.machine_id for o in op.options} for op in _operations(problem)}
    for machine_id, seq in sequences.items():
        bad = [op_id for op_id in seq if machine_id not in options[op_id]]
        if bad:
            raise ValueError(f"machine {machine_id} is not an option of {bad}")


# --- semi-active placement ---------------------------------------------------------------------


def _operations(problem: Problem) -> Iterator[Operation]:
    return (op for job in problem.jobs for op in job.operations)


class _Placer:
    """Places operations one at a time; each start depends only on already placed ones."""

    def __init__(self, problem: Problem) -> None:
        self._problem = problem
        self._setups = SetupCalculator(problem)
        self._ops = {op.id: op for op in _operations(problem)}
        self._release = {op.id: job.release for job in problem.jobs for op in job.operations}
        self._available = {m.id: m.state.available_from for m in problem.machines}
        self._restart = {m.id: m.restart for m in problem.machines}
        self.placed: dict[str, Assignment] = {}

    def ready(self, op_id: str) -> bool:
        return all(p.operation_id in self.placed for p in self._ops[op_id].predecessors)

    def place(self, machine_id: str, prev_id: str | None, op_id: str) -> None:
        op = self._ops[op_id]
        setup = self._setups.seconds(machine_id, prev_id, op_id)
        idle_since = max(self._problem.now, self._available[machine_id])
        if prev_id is not None:
            idle_since = self.placed[prev_id].end
        machine_free = max(self._problem.now, self._available[machine_id], idle_since)
        gate = self._material_gate(op)
        start = max(machine_free, gate - setup if op.setup_detached else gate)
        setup += self._warmup(machine_id, start - idle_since)
        option = next(o for o in op.options if o.machine_id == machine_id)
        run = option.fixed_seconds + op.quantity * option.unit_seconds
        self.placed[op_id] = Assignment(
            operation_id=op_id,
            machine_id=machine_id,
            start=start,
            setup_end=start + setup,
            end=start + setup + run,
        )

    def _warmup(self, machine_id: str, idle: float) -> float:
        """D-074: the machine's warm-up seconds when `idle` is longer than its threshold."""
        rule = self._restart[machine_id]
        if rule is not None and idle > rule.idle_threshold_seconds:
            return rule.warmup_seconds
        return 0.0

    def _material_gate(self, op: Operation) -> float:
        """Earliest time the material is there: now, release, ready time, predecessor + lag."""
        after_predecessors = (self.placed[p.operation_id].end + p.min_lag for p in op.predecessors)
        return max(self._problem.now, self._release[op.id], op.ready_time, *after_predecessors)


def semi_active(problem: Problem, sequences: Sequences) -> Schedule:
    """The semi-active schedule for per-machine operation sequences (machine id -> op ids).

    Every operation starts as early as its machine order, its sequence-dependent setup
    (from the machine predecessor, or the machine state for the first), `now`, the
    machine's `available_from` and its material gate allow. The gate is the latest of
    `now`, job release, `ready_time` and each predecessor's end plus `min_lag`; with an
    attached setup the start waits for it, with a detached setup only `setup_end` does
    (D-063). A machine with a restart rule adds its warm-up to the setup phase when it
    idled longer than the threshold before `start`, measured from the previous op's end
    or from max(now, available_from) for the first (D-074). The start is chosen first
    from the setup alone, so a warm-up never pulls a detached start earlier; it only
    moves `setup_end` and `end`. Machines missing from `sequences` get no operations.

    Raises ValueError for unsupported features or sequences that do not list each
    operation once on one of its options, and SequenceDeadlock when the sequences
    contradict precedence.
    """
    _check_supported(problem)
    _check_sequences(problem, sequences)
    placer = _Placer(problem)
    position = dict.fromkeys(sequences, 0)
    progress = True
    while progress:
        progress = False
        for machine_id, seq in sequences.items():
            while position[machine_id] < len(seq) and placer.ready(seq[position[machine_id]]):
                index = position[machine_id]
                prev_id = seq[index - 1] if index > 0 else None
                placer.place(machine_id, prev_id, seq[index])
                position[machine_id] += 1
                progress = True
    if len(placer.placed) < sum(len(seq) for seq in sequences.values()):
        raise SequenceDeadlock("the machine sequences contradict precedence")
    ordered = [placer.placed[op_id] for seq in sequences.values() for op_id in seq]
    return Schedule(
        status="FEASIBLE", assignments=tuple(ordered), solver="oracle", elapsed_seconds=0.0
    )


# --- enumeration -------------------------------------------------------------------------------


def _option_choices(problem: Problem) -> Iterator[dict[str, list[str]]]:
    """Every choice of one option per operation, as machine id -> op ids in problem order."""
    ops = list(_operations(problem))
    for machines in itertools.product(*([o.machine_id for o in op.options] for op in ops)):
        grouped: dict[str, list[str]] = {m.id: [] for m in problem.machines}
        for op, machine_id in zip(ops, machines, strict=True):
            grouped[machine_id].append(op.id)
        yield grouped


def combination_count(problem: Problem, limit: int = MAX_COMBINATIONS) -> int:
    """Sum over option choices of the product of per-machine sequence counts.

    Counting stops as soon as the total passes `limit`, so the result is exact up to
    `limit` and some value above it otherwise.
    """
    total = 0
    for grouped in _option_choices(problem):
        total += math.prod(math.factorial(len(ops)) for ops in grouped.values())
        if total > limit:
            break
    return total


def all_sequences(problem: Problem) -> Iterator[dict[str, tuple[str, ...]]]:
    """Every option choice with every per-machine order, in a deterministic order.

    Sequences may contradict precedence; `semi_active` raises `SequenceDeadlock` for those.
    """
    for grouped in _option_choices(problem):
        machine_ids = list(grouped)
        orders = (itertools.permutations(grouped[m]) for m in machine_ids)
        for combo in itertools.product(*orders):
            yield dict(zip(machine_ids, combo, strict=True))


def brute_force(
    problem: Problem, objective: ObjectiveKind, profile: SchedulingProfile | None = None
) -> tuple[float, Schedule]:
    """The best `objective` value over every option choice and machine sequence.

    Enumeration order is deterministic (options in problem order, then permutations in
    `itertools` order); among equal values the first found wins. Maximize-sense objectives
    (robustness) are maximized. Deadlocked sequences are skipped. Job weights come from
    `weight_override`, else the profile's class weights (via `objective_values`). The returned
    schedule has status OPTIMAL and `primary_value` set.

    Raises ValueError above MAX_COMBINATIONS combinations, for unsupported features, or
    when no sequence is feasible.
    """
    _check_supported(problem)
    if combination_count(problem) > MAX_COMBINATIONS:
        raise ValueError(f"more than {MAX_COMBINATIONS} combinations to enumerate")
    compiled = CompiledProblem(problem=problem, profile=profile or SchedulingProfile())
    sign = -1.0 if objective in MAXIMIZED else 1.0
    best: tuple[float, Schedule] | None = None
    for sequences in all_sequences(problem):
        try:
            schedule = semi_active(problem, sequences)
        except SequenceDeadlock:
            continue
        value = objective_values(compiled, schedule)[objective]
        if best is None or sign * value < sign * best[0]:
            best = (value, schedule)
    if best is None:
        raise ValueError("no sequence gives a feasible schedule")
    value, schedule = best
    return value, schedule.model_copy(update={"status": "OPTIMAL", "primary_value": value})
