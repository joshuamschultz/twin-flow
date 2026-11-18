"""Setup oracle: sequence-dependent setup seconds on a machine (COMP-002, REQ-064, D-064).

The setup before operation ``next`` on machine i depends on what the machine did last:
the previous operation, or the machine's ``MachineState`` when ``next`` is first. The
machine's function is its own ``setup_function_id``, else its work center's, else none
(setup 0). A function resolves in this order (D-064):

1. Parameters: when the function has terms and both sides carry every parameter the
   terms need, the value is h_i(params_prev, params_next), the terms combined by ``sum``
   (sequential changeovers) or ``max`` (parallel changeovers, Pinedo p.460).
   ``abs_diff`` needs a numeric value on both sides; a string counts as missing.
2. Family matrix: the entry for (family_prev, family_next), when both families are set
   and the entry exists. Equal families get no implicit 0; add the diagonal entry.
3. ``default_seconds``.

A function with no terms is decided by steps 2 and 3 only (a pure family matrix).
``SetupTable`` memoizes values by ids for solvers and local search (Pinedo p.458-461);
``cache_info()`` reports its hits, misses (computations) and size (T-146, REQ-111).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from twinflow.scheduling.model import (
    AbsDiffTerm,
    MachineState,
    MismatchTerm,
    Operation,
    Problem,
    SetupFunction,
    SetupParams,
)

SetupSide = Operation | MachineState
"""What the machine did before: the previous operation or its state at `now`."""


def _params(side: SetupSide) -> SetupParams:
    return side.setup_params if isinstance(side, Operation) else side.last_setup_params


def _family(side: SetupSide) -> str | None:
    return side.setup_family if isinstance(side, Operation) else side.last_setup_family


def _term_seconds(
    term: AbsDiffTerm | MismatchTerm, prev: SetupParams, nxt: SetupParams
) -> float | None:
    """One term's seconds, or None when a side lacks the value the term needs."""
    if term.param not in prev or term.param not in nxt:
        return None
    a, b = prev[term.param], nxt[term.param]
    if isinstance(term, MismatchTerm):
        return term.seconds if a != b else 0.0
    if isinstance(a, str) or isinstance(b, str):
        return None
    return term.seconds_per_unit * abs(b - a)


def _parameter_seconds(function: SetupFunction, prev: SetupSide, nxt: SetupSide) -> float | None:
    """h_i(params_prev, params_next), or None when the terms cannot decide (D-064 step 1)."""
    if not function.terms:
        return None
    values = [_term_seconds(term, _params(prev), _params(nxt)) for term in function.terms]
    known = [value for value in values if value is not None]
    if len(known) < len(values):
        return None
    return max(known) if function.combine == "max" else sum(known)


def _family_seconds(function: SetupFunction, prev: SetupSide, nxt: SetupSide) -> float | None:
    """The matrix entry for (family_prev, family_next), or None (D-064 step 2)."""
    prev_family, next_family = _family(prev), _family(nxt)
    if prev_family is None or next_family is None:
        return None
    for entry in function.family_matrix:
        if entry.from_family == prev_family and entry.to_family == next_family:
            return entry.seconds
    return None


def setup_seconds(function: SetupFunction | None, prev: SetupSide, nxt: Operation) -> float:
    """Setup seconds (>= 0) before ``nxt`` when ``prev`` came last; 0 with no function.

    Direct, uncached computation of the D-064 resolution order (see the module docstring).
    """
    if function is None:
        return 0.0
    by_parameters = _parameter_seconds(function, prev, nxt)
    if by_parameters is not None:
        return by_parameters
    by_family = _family_seconds(function, prev, nxt)
    if by_family is not None:
        return by_family
    return function.default_seconds


class SetupCacheInfo(NamedTuple):
    """Setup cache counters: lookups served from the cache, computations, cached triples."""

    hits: int
    misses: int
    size: int


class SetupTable:
    """Memoized setup seconds for one problem, keyed by ids (D-064, Pinedo p.458-461).

    Build once with ``for_problem``; ``seconds(machine_id, prev_id, next_id)`` computes each
    (machine, previous, next) triple once and returns the cached value afterwards. The
    values equal ``setup_seconds`` for the same inputs. Unknown ids raise KeyError.
    """

    __slots__ = ("_cache", "_functions", "_hits", "_misses", "_operations", "_states")

    def __init__(
        self,
        functions: Mapping[str, SetupFunction | None],
        states: Mapping[str, MachineState],
        operations: Mapping[str, Operation],
    ) -> None:
        self._functions = dict(functions)
        self._states = dict(states)
        self._operations = dict(operations)
        self._cache: dict[tuple[str, str | None, str], float] = {}
        self._hits = 0
        self._misses = 0

    @classmethod
    def for_problem(cls, problem: Problem) -> SetupTable:
        """Resolve every machine's function and index every operation by id, once."""
        centers = {center.id: center for center in problem.work_centers}
        functions_by_id = {fn.id: fn for fn in problem.setup_functions}
        functions: dict[str, SetupFunction | None] = {}
        for machine in problem.machines:
            function_id = (
                machine.setup_function_id or centers[machine.work_center_id].setup_function_id
            )
            functions[machine.id] = functions_by_id[function_id] if function_id else None
        operations = {op.id: op for job in problem.jobs for op in job.operations}
        states = {machine.id: machine.state for machine in problem.machines}
        return cls(functions, states, operations)

    def function_for(self, machine_id: str) -> SetupFunction | None:
        """The setup function resolved for the machine (None means setup is always 0)."""
        return self._functions[machine_id]

    def cache_info(self) -> SetupCacheInfo:
        """Hits, misses (each a computation, so misses == size) and cached triples."""
        return SetupCacheInfo(self._hits, self._misses, len(self._cache))

    def seconds(self, machine_id: str, prev_id: str | None, next_id: str) -> float:
        """Setup seconds before ``next_id`` on the machine; ``prev_id`` None = machine state."""
        key = (machine_id, prev_id, next_id)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        prev = self._states[machine_id] if prev_id is None else self._operations[prev_id]
        value = setup_seconds(self._functions[machine_id], prev, self._operations[next_id])
        self._cache[key] = value
        self._misses += 1
        return value
