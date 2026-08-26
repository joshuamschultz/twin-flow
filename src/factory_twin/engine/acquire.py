"""COMP-002 ResourceAcquirer — the deadlock guard.

Acquires machine then operator in one fixed global order and manually releases any leg
already held when a wait is abandoned. Every dual-resource path routes through here.
"""

from __future__ import annotations

from collections.abc import Generator

import simpy
from simpy.resources.resource import PriorityRequest


class ResourceAcquirer:
    """The single code path for a machine+operator dual acquire (D-045 deadlock guard).

    Acquires the machine leg, then the operator leg, in that fixed order — never the
    reverse — so SimPy's `AllOf` (which provides no two-phase locking) cannot deadlock
    two callers that would otherwise acquire in opposite orders (structure.md,
    "Acquisition order is global and fixed: machine, then operator"). If the
    operator-leg wait is abandoned (`simpy.Interrupt`), the already-granted machine leg
    is released and the still-queued operator request is cancelled before the
    interrupt is re-raised, so no partial hold and no leaked queue entry survive the
    abandon.
    """

    def __init__(
        self, machine_pool: simpy.PriorityResource, labor_pool: simpy.PriorityResource
    ) -> None:
        self._machine_pool = machine_pool
        self._labor_pool = labor_pool

    def acquire(
        self, priority: int
    ) -> Generator[simpy.Event, None, tuple[PriorityRequest, PriorityRequest]]:
        """Acquire machine then operator, in that fixed order (D-045).

        Drive with `yield from`. On success, returns `(machine_request,
        operator_request)` — release each directly via `pool.release(request)`. On
        abandon (a `simpy.Interrupt` raised while waiting on the operator leg),
        releases the already-held machine leg, cancels the still-queued operator
        request so it does not inflate the labor pool's wait queue, then re-raises the
        interrupt.
        """
        machine_request = self._machine_pool.request(priority=priority)
        yield machine_request

        operator_request = self._labor_pool.request(priority=priority)
        try:
            yield operator_request
        except simpy.Interrupt:
            operator_request.cancel()
            self._labor_pool.release(operator_request)
            self._machine_pool.release(machine_request)
            raise

        return machine_request, operator_request
