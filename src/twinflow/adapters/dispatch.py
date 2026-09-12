"""Dispatch policies — the seam that decides which queued job a center runs next.

Wiring a policy into the engine's queues is a separate, later job (active scheduling);
what ships here is the surface and the classic policies, as pure functions over a queue
of jobs, so they are ready to plug in and are unit-testable on their own. A policy is
`select(queue, now) -> the chosen job`; `order` gives the whole sequence.

FIFO (arrival order), EDD (earliest due date), SPT (shortest processing time), and
critical ratio (slack per unit work) are built in; a custom rule registers the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from twinflow.modules.registry import Registry


@dataclass(frozen=True)
class DispatchJob:
    """One queued job as a dispatch rule sees it."""

    job_id: str
    arrival_time: float
    due_date: float
    processing_time: float


@runtime_checkable
class DispatchPolicy(Protocol):
    """Choose the next job to run from a non-empty queue at time `now`."""

    @property
    def name(self) -> str: ...

    def select(self, queue: Sequence[DispatchJob], now: float) -> DispatchJob: ...

    def order(self, queue: Sequence[DispatchJob], now: float) -> list[DispatchJob]: ...


@dataclass(frozen=True)
class KeyedPolicy:
    """A dispatch policy defined by a sort key — smaller key runs first."""

    name: str
    key: Callable[[DispatchJob, float], float]

    def select(self, queue: Sequence[DispatchJob], now: float) -> DispatchJob:
        if not queue:
            raise ValueError("cannot select from an empty queue")
        return min(queue, key=lambda job: self.key(job, now))

    def order(self, queue: Sequence[DispatchJob], now: float) -> list[DispatchJob]:
        return sorted(queue, key=lambda job: self.key(job, now))


def _critical_ratio(job: DispatchJob, now: float) -> float:
    """(due - now) / processing_time — slack per unit of work; smaller is more
    urgent. A job at or past due, or with no processing time, is maximally urgent."""
    if job.processing_time <= 0:
        return float("-inf")
    return (job.due_date - now) / job.processing_time


DISPATCH_POLICIES: Registry[DispatchPolicy] = Registry("dispatch_policy")
DISPATCH_POLICIES.register("fifo", lambda: KeyedPolicy("fifo", lambda job, now: job.arrival_time))
DISPATCH_POLICIES.register("edd", lambda: KeyedPolicy("edd", lambda job, now: job.due_date))
DISPATCH_POLICIES.register("spt", lambda: KeyedPolicy("spt", lambda job, now: job.processing_time))
DISPATCH_POLICIES.register("critical_ratio", lambda: KeyedPolicy("critical_ratio", _critical_ratio))
