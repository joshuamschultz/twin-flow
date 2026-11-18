"""Feature derivation: which `Feature`s a problem uses (D-067).

`solve()` refuses a solver that does not declare every feature the problem uses. A solver
with a restricted capability set (CP-SAT) also checks here itself, because composite
methods (reschedule's improver, rolling_horizon's seam and window solvers) call it through
the `Solver` seam past `solve()`'s check. Imports only the model, the setup table and the
seam, so solver modules may import it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from twinflow.scheduling.model import Machine, Operation, Precedence, Problem
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.setups import SetupTable


def problem_features(problem: Problem) -> frozenset[Feature]:
    """Every `Feature` the problem uses; operations in state `done` are skipped.

    The problem must be valid (`validate` returns no issue). The rules:

    - alternatives: an operation with more than one option.
    - setups: an option machine of an operation resolves a setup function (its own, else
      its work center's, D-064).
    - detached_setup: a `setup_detached` operation that has `setups` by the rule above. A
      resolved function is taken as "can get a non-zero setup" without inspecting its
      values, so a detached op always comes with `setups` and a flag on a setup-free
      problem triggers no refusal.
    - calendars: a machine with a calendar, or a pool with a calendar that an operation
      draws operators from.
    - pause: an operation with `shift_crossing` pause where a calendar can apply to it: an
      option machine has a calendar, or a pool it draws operators from has one. Without
      such a calendar the op never crosses a closed period, so pause changes nothing.
    - unattended: an operation with `shift_crossing` finish_unattended.
    - operators / pins / in_progress: an operation with operator needs, a pin, or state
      `in_progress`.
    - batching: an operation with a `batch_family`. A batch machine alone is not batching:
      operations without a family run on it one at a time.
    - restart: a machine with a restart rule.
    - lags / max_lags / overlap: a precedence with `min_lag` > 0, a `max_lag`, or an
      `overlap_quantity`.
    - machine_state: a machine available only after `now`, or with a last setup (params or
      family) that can change the first setup.
    """
    table = SetupTable.for_problem(problem)
    operations = [op for job in problem.jobs for op in job.operations if op.status.state != "done"]
    calendared = _Calendared.of(problem)
    features: set[Feature] = set()
    for op in operations:
        features |= _operation_features(op, table)
        if op.shift_crossing == "pause" and calendared.apply_to(op):
            features.add(Feature.PAUSE)
    for machine in problem.machines:
        features |= _machine_features(machine, problem.now)
    if _uses_pool_calendar(problem, operations):
        features.add(Feature.CALENDARS)
    return frozenset(features)


def _operation_features(op: Operation, table: SetupTable) -> set[Feature]:
    has_setup = any(table.function_for(option.machine_id) is not None for option in op.options)
    flags = {
        Feature.ALTERNATIVES: len(op.options) > 1,
        Feature.SETUPS: has_setup,
        Feature.DETACHED_SETUP: op.setup_detached and has_setup,
        Feature.UNATTENDED: op.shift_crossing == "finish_unattended",
        Feature.OPERATORS: bool(op.operator_needs),
        Feature.BATCHING: op.batch_family is not None,
        Feature.PINS: op.pin is not None,
        Feature.IN_PROGRESS: op.status.state == "in_progress",
    }
    return _present(flags) | _precedence_features(op.predecessors)


def _precedence_features(links: Sequence[Precedence]) -> set[Feature]:
    flags = {
        Feature.LAGS: any(link.min_lag > 0 for link in links),
        Feature.MAX_LAGS: any(link.max_lag is not None for link in links),
        Feature.OVERLAP: any(link.overlap_quantity is not None for link in links),
    }
    return _present(flags)


def _machine_features(machine: Machine, now: float) -> set[Feature]:
    state = machine.state
    flags = {
        Feature.CALENDARS: machine.calendar_id is not None,
        Feature.RESTART: machine.restart is not None,
        Feature.MACHINE_STATE: state.available_from > now
        or bool(state.last_setup_params)
        or state.last_setup_family is not None,
    }
    return _present(flags)


@dataclass(frozen=True, slots=True)
class _Calendared:
    """Ids of machines and of operator pools that carry a calendar (kept apart: ids may clash)."""

    machines: frozenset[str]
    pools: frozenset[str]

    @classmethod
    def of(cls, problem: Problem) -> _Calendared:
        machines = frozenset(m.id for m in problem.machines if m.calendar_id is not None)
        pools = frozenset(p.id for p in problem.operator_pools if p.calendar_id is not None)
        return cls(machines, pools)

    def apply_to(self, op: Operation) -> bool:
        """True when an option machine of `op` or a pool it draws from has a calendar."""
        return any(option.machine_id in self.machines for option in op.options) or any(
            need.pool_id in self.pools for need in op.operator_needs
        )


def _uses_pool_calendar(problem: Problem, operations: Iterable[Operation]) -> bool:
    used = {need.pool_id for op in operations for need in op.operator_needs}
    return any(pool.id in used and pool.calendar_id is not None for pool in problem.operator_pools)


def _present(flags: dict[Feature, bool]) -> set[Feature]:
    return {feature for feature, present in flags.items() if present}
