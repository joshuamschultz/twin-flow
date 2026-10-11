"""Brute-force test oracle for small scheduling problems (COMP-012, REQ-078, REQ-079).

`semi_active` places every operation as early as possible for fixed per-machine
sequences; `brute_force` enumerates every option choice and every per-machine sequence
and keeps the best schedule by `oracle_math.objective_values`. The oracle is deliberately small:
it supports releases, ready times, machine state, sequence-dependent setups (attached
and detached, D-063), restart warm-ups (D-074) and end-to-gate precedence with `min_lag`.
Problems using calendars, operators, batching, pins, shop-floor status, held jobs,
transfer-lot overlap or `max_lag` raise ValueError, so a test can never trust an oracle
that ignored a constraint. `semi_active` is not exact under restart (waiting can save a
warm-up); `brute_force_least` (bottom of the module) is, and also supports transfer-lot
overlap and batches.
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


# --- least schedules: overlap, batches and restart (T-136, T-137) ------------------------------
#
# `semi_active` starts every operation as early as possible. With a restart rule that is not
# always optimal: delaying an operation can keep the next idle within the threshold and save
# a warm-up, so a later start can finish everything sooner. The functions below are exact for
# every objective that never gets worse when an operation ends earlier (all but
# earliness-tardiness and churn). For fixed machine sequences of blocks (a block is one
# operation, or one batch on a batch machine) and a fixed warm-up choice per block, every
# constraint is a difference constraint `x_v >= x_u + w` over block starts:
#
# - start >= max(now, available_from); material gate >= max(release, ready_time), the gate is
#   `start` (attached setup) or `setup_end` (detached setup);
# - a block starts after the previous block on its machine ends;
# - end-to-gate precedence with `min_lag`; with a transfer lot q (D-061):
#   gate >= pred.setup_end + pred.fixed + q * pred.unit + min_lag and
#   end >= pred.end + q * succ.unit;
# - restart (D-074): idle = start - (previous block end, or max(now, available_from)); a
#   "warm" block has idle >= threshold + TICK and its setup phase grows by the warm-up, a
#   "cold" block has idle <= threshold.
#
# The least solution (longest paths from time 0, Bellman-Ford) is the earliest start of every
# block at once, so it is the best schedule for that (sequence, warm-up choice); enumerating
# every option choice, batch partition, block order and warm-up choice gives the optimum.
# Batch members share start, setup_end and end, have one non-null `batch_family`, a run of
# equal length and a total quantity within the machine's batch capacity.

TICK = 1e-6
"""Smallest idle step past a restart threshold (one CP-SAT tick); warm-up needs idle > threshold."""
MAX_BATCH_OPS = 6
"""Most batch-family operations one batch machine may hold in a least-schedule enumeration."""

Block = tuple[str, ...]
BlockSequences = Mapping[str, Sequence[Block]]
NON_REGULAR = frozenset({ObjectiveKind.EARLINESS_TARDINESS, ObjectiveKind.CHURN})
"""Objectives an earlier end can make worse: the least schedule is not their optimum."""


def _machine_setup_function(problem: Problem, machine_id: str) -> str | None:
    machine = next(m for m in problem.machines if m.id == machine_id)
    centers = {c.id: c.setup_function_id for c in problem.work_centers}
    return machine.setup_function_id or centers.get(machine.work_center_id)


def _least_unsupported(problem: Problem) -> list[str]:
    found = [f"machine {m.id} calendar" for m in problem.machines if m.calendar_id]
    found += [
        f"batch machine {m.id} with a setup function"
        for m in problem.machines
        if m.batch and _machine_setup_function(problem, m.id)
    ]
    found += [f"job {j.id} status {j.status}" for j in problem.jobs if j.status != "open"]
    for op in _operations(problem):
        checks = {
            "operator_needs": bool(op.operator_needs),
            "pin": op.pin is not None,
            "status": op.status.state != "pending",
            "max_lag": any(p.max_lag is not None for p in op.predecessors),
        }
        found += [f"operation {op.id} {name}" for name, used in checks.items() if used]
    return found


def _check_least_supported(problem: Problem) -> None:
    found = _least_unsupported(problem)
    if found:
        raise ValueError(f"oracle does not support: {', '.join(found)}")


class _Least:
    """Difference constraints over block starts for one (sequences, warm-up choice)."""

    def __init__(self, problem: Problem, sequences: BlockSequences, warm: Mapping[Block, bool]):
        self._problem = problem
        self._setups = SetupCalculator(problem)
        self._ops = {op.id: op for op in _operations(problem)}
        self._job = {op.id: job for job in problem.jobs for op in job.operations}
        self._machines = {m.id: m for m in problem.machines}
        self._sequences = sequences
        self._warm = warm
        self.blocks: list[tuple[str, Block]] = [
            (machine_id, block) for machine_id, seq in sequences.items() for block in seq
        ]
        self._node = {block: k + 1 for k, (_, block) in enumerate(self.blocks)}
        self._block_of = {op_id: block for _, block in self.blocks for op_id in block}
        self.edges: list[tuple[int, int, float]] = []
        self.setup: dict[Block, float] = {}
        self.warmup: dict[Block, float] = {}
        self.run: dict[Block, float] = {}

    def _option(self, op_id: str, machine_id: str) -> tuple[float, float]:
        option = next(o for o in self._ops[op_id].options if o.machine_id == machine_id)
        return option.fixed_seconds, option.unit_seconds

    def _run(self, op_id: str, machine_id: str) -> float:
        fixed, unit = self._option(op_id, machine_id)
        return fixed + self._ops[op_id].quantity * unit

    def _machine_of(self, op_id: str) -> str:
        return next(m for m, block in self.blocks if op_id in block)

    def duration(self, block: Block) -> float:
        return self.setup[block] + self.warmup[block] + self.run[block]

    def build(self) -> None:
        for machine_id, seq in self._sequences.items():
            self._add_machine(machine_id, seq)
        for _, block in self.blocks:
            for op_id in block:
                self._add_precedence(op_id)

    def _add_machine(self, machine_id: str, seq: Sequence[Block]) -> None:
        machine = self._machines[machine_id]
        base = max(self._problem.now, machine.state.available_from)
        previous: Block | None = None
        for block in seq:
            self._check_block(machine_id, block)
            prev_op = previous[-1] if previous else None
            self.setup[block] = self._setups.seconds(machine_id, prev_op, block[0])
            self.run[block] = self._run(block[0], machine_id)
            warm = machine.restart is not None and self._warm.get(block, False)
            self.warmup[block] = machine.restart.warmup_seconds if warm and machine.restart else 0.0
            node = self._node[block]
            self.edges.append((0, node, base))
            for op_id in block:
                self._add_readiness(op_id, block)
            if previous is not None:
                self.edges.append((self._node[previous], node, self.duration(previous)))
            if machine.restart is not None:
                self._add_restart(block, previous, base, machine.restart.idle_threshold_seconds)
            previous = block

    def _check_block(self, machine_id: str, block: Block) -> None:
        if len(block) == 1:
            return
        machine = self._machines[machine_id]
        families = {self._ops[o].batch_family for o in block}
        runs = {self._run(o, machine_id) for o in block}
        load = math.fsum(self._ops[o].quantity for o in block)
        if machine.batch is None or None in families or len(families) != 1:
            raise ValueError(f"block {block} on {machine_id} is not one batch family")
        if len(runs) != 1 or load > machine.batch.capacity:
            raise ValueError(f"block {block} on {machine_id} breaks run length or capacity")

    def _detached(self, op_id: str, block: Block) -> float:
        """Seconds from start to the material gate: the setup phase when detached, else 0."""
        if self._ops[op_id].setup_detached:
            return self.setup[block] + self.warmup[block]
        return 0.0

    def _add_readiness(self, op_id: str, block: Block) -> None:
        op = self._ops[op_id]
        ready = max(self._job[op_id].release, op.ready_time)
        self.edges.append((0, self._node[block], ready - self._detached(op_id, block)))

    def _add_restart(
        self, block: Block, previous: Block | None, base: float, threshold: float
    ) -> None:
        """Warm: idle >= threshold + TICK. Cold: idle <= threshold (an edge back)."""
        node = self._node[block]
        if self.warmup[block] > 0:
            if previous is None:
                self.edges.append((0, node, base + threshold + TICK))
            else:
                gap = self.duration(previous) + threshold + TICK
                self.edges.append((self._node[previous], node, gap))
            return
        if previous is None:
            self.edges.append((node, 0, -(base + threshold)))
        else:
            self.edges.append((node, self._node[previous], -(threshold + self.duration(previous))))

    def _add_precedence(self, op_id: str) -> None:
        block = self._block_of[op_id]
        machine_id = self._machine_of(op_id)
        node = self._node[block]
        detached = self._detached(op_id, block)
        for link in self._ops[op_id].predecessors:
            pred_block = self._block_of[link.operation_id]
            pred_node = self._node[pred_block]
            if link.overlap_quantity is None:
                weight = self.duration(pred_block) + link.min_lag - detached
                self.edges.append((pred_node, node, weight))
                continue
            q = link.overlap_quantity
            pred_fixed, pred_unit = self._option(link.operation_id, self._machine_of(pred_block[0]))
            transfer = self.setup[pred_block] + self.warmup[pred_block] + pred_fixed + q * pred_unit
            self.edges.append((pred_node, node, transfer + link.min_lag - detached))
            _, unit = self._option(op_id, machine_id)
            last_lot = self.duration(pred_block) + q * unit - self.duration(block)
            self.edges.append((pred_node, node, last_lot))

    def starts(self) -> list[float] | None:
        """Longest paths from node 0 (time 0); None when a positive cycle makes it infeasible."""
        count = len(self.blocks) + 1
        dist = [0.0] + [-math.inf] * (count - 1)
        for _ in range(count + 1):
            changed = False
            for u, v, w in self.edges:
                if dist[u] + w > dist[v] + 1e-12:
                    dist[v] = dist[u] + w
                    changed = True
            if dist[0] > 1e-12:
                return None
            if not changed:
                return dist
        return None


def least_schedule(
    problem: Problem, sequences: BlockSequences, warm: Mapping[Block, bool]
) -> Schedule | None:
    """The earliest schedule for machine block sequences and a warm-up choice per block.

    `sequences` maps machine id -> blocks in order; a block is a tuple of op ids (one op, or
    the members of one batch on a batch machine). `warm` says, per block on a machine with a
    restart rule, whether it pays the warm-up (missing means cold). Returns None when the
    choice is infeasible (precedence against the sequence, or a cold block that cannot keep
    its idle within the threshold). Raises ValueError for unsupported features, a sequence
    that does not list every op once on one of its options, or an invalid batch.
    """
    _check_least_supported(problem)
    flat = {m: [o for block in seq for o in block] for m, seq in sequences.items()}
    _check_sequences(problem, flat)
    least = _Least(problem, sequences, warm)
    least.build()
    dist = least.starts()
    if dist is None:
        return None
    return Schedule(
        status="FEASIBLE",
        assignments=tuple(_block_assignments(problem, least, dist)),
        solver="oracle",
        elapsed_seconds=0.0,
    )


def _block_assignments(problem: Problem, least: _Least, dist: Sequence[float]) -> list[Assignment]:
    batch_machines = {m.id for m in problem.machines if m.batch is not None}
    families = {op.id: op.batch_family for op in _operations(problem)}
    opened: dict[str, int] = {}
    assignments: list[Assignment] = []
    for k, (machine_id, block) in enumerate(least.blocks, start=1):
        start = dist[k]
        setup_end = start + least.setup[block] + least.warmup[block]
        batch_id = None
        if machine_id in batch_machines and families[block[0]] is not None:
            opened[machine_id] = opened.get(machine_id, 0) + 1
            batch_id = f"{machine_id}#b{opened[machine_id]}"
        assignments += [
            Assignment(
                operation_id=op_id,
                machine_id=machine_id,
                start=start,
                setup_end=setup_end,
                end=setup_end + least.run[block],
                batch_id=batch_id,
            )
            for op_id in block
        ]
    return assignments


def _partitions(items: Sequence[str]) -> Iterator[list[Block]]:
    """Every set partition of `items` (Bell number many)."""
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for part in _partitions(rest):
        yield [(first,), *part]
        for k in range(len(part)):
            yield [*part[:k], (first, *part[k]), *part[k + 1 :]]


def _batchable(problem: Problem, machine_id: str, block: Block) -> bool:
    if len(block) == 1:
        return True
    machine = next(m for m in problem.machines if m.id == machine_id)
    ops = {op.id: op for op in _operations(problem)}
    families = {ops[o].batch_family for o in block}
    runs = {
        next(
            opt.fixed_seconds + ops[o].quantity * opt.unit_seconds
            for opt in ops[o].options
            if opt.machine_id == machine_id
        )
        for o in block
    }
    load = math.fsum(ops[o].quantity for o in block)
    capacity = machine.batch.capacity if machine.batch else 0.0
    return None not in families and len(families) == 1 and len(runs) == 1 and load <= capacity


def _machine_orders(problem: Problem, machine_id: str, op_ids: Sequence[str]) -> list[list[Block]]:
    """Every block order of `op_ids` on one machine: batch partitions on a batch machine."""
    machine = next(m for m in problem.machines if m.id == machine_id)
    if machine.batch is None:
        return [[(o,) for o in order] for order in itertools.permutations(op_ids)]
    if len(op_ids) > MAX_BATCH_OPS:
        raise ValueError(f"more than {MAX_BATCH_OPS} operations on batch machine {machine_id}")
    orders: list[list[Block]] = []
    for partition in _partitions(list(op_ids)):
        if all(_batchable(problem, machine_id, block) for block in partition):
            orders += [list(order) for order in itertools.permutations(partition)]
    return orders


def all_block_sequences(problem: Problem) -> Iterator[dict[str, tuple[Block, ...]]]:
    """Every option choice, batch partition and block order, in a deterministic order."""
    for grouped in _option_choices(problem):
        machine_ids = list(grouped)
        orders = [_machine_orders(problem, m, grouped[m]) for m in machine_ids]
        for combo in itertools.product(*orders):
            yield {m: tuple(seq) for m, seq in zip(machine_ids, combo, strict=True)}


def _warm_choices(problem: Problem, sequences: BlockSequences) -> Iterator[dict[Block, bool]]:
    restart = {m.id for m in problem.machines if m.restart is not None}
    blocks = [b for m, seq in sequences.items() if m in restart for b in seq]
    for flags in itertools.product((False, True), repeat=len(blocks)):
        yield dict(zip(blocks, flags, strict=True))


def least_schedules(problem: Problem, limit: int = MAX_COMBINATIONS) -> Iterator[Schedule]:
    """The least schedule of every feasible (block sequences, warm-up choice).

    Raises ValueError for unsupported features or above `limit` combinations tried.
    """
    _check_least_supported(problem)
    tried = 0
    for sequences in all_block_sequences(problem):
        for warm in _warm_choices(problem, sequences):
            tried += 1
            if tried > limit:
                raise ValueError(f"more than {limit} combinations to enumerate")
            schedule = least_schedule(problem, sequences, warm)
            if schedule is not None:
                yield schedule


def brute_force_least(
    problem: Problem, objective: ObjectiveKind, profile: SchedulingProfile | None = None
) -> tuple[float, Schedule]:
    """The optimum of a regular `objective` with overlap, batches and restart supported.

    Like `brute_force` (first best wins, robustness maximized), over `least_schedules`.
    Raises ValueError for earliness-tardiness and churn (an earlier end can be worse),
    unsupported features, too many combinations, or no feasible schedule.
    """
    if objective in NON_REGULAR:
        raise ValueError(f"least schedules are not exact for {objective.value}")
    compiled = CompiledProblem(problem=problem, profile=profile or SchedulingProfile())
    sign = -1.0 if objective in MAXIMIZED else 1.0
    best: tuple[float, Schedule] | None = None
    for schedule in least_schedules(problem):
        value = objective_values(compiled, schedule)[objective]
        if best is None or sign * value < sign * best[0]:
            best = (value, schedule)
    if best is None:
        raise ValueError("no sequence gives a feasible schedule")
    value, schedule = best
    return value, schedule.model_copy(update={"status": "OPTIMAL", "primary_value": value})
