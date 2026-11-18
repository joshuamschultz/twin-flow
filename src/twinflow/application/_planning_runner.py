"""Bounded solve execution for the planning service (COMP-117, REQ-169, D-120).

Solves run on a fixed-size thread pool. The caller waits at most budget + grace seconds;
past that it gets `PlanningTimeout` and stops waiting. A running solver thread cannot be
killed, so its worker is released when the solver returns (TF-003 honors its budget); a
solve still queued behind busy workers is cancelled instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from twinflow.application._planning_models import PlanningTimeout

T = TypeVar("T")

log = logging.getLogger(__name__)


class SolveRunner:
    """Run one solve on the bounded pool with a hard wall-clock limit."""

    def __init__(self, *, max_workers: int, grace_seconds: float) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if not grace_seconds >= 0:
            raise ValueError("timeout_grace_seconds must be 0 or more")
        self._grace_seconds = grace_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="twinflow-plan")

    def run(self, solve: Callable[[], T], budget_seconds: float) -> T:
        """`solve()`'s result, or `PlanningTimeout` after budget + grace seconds."""
        future = self._pool.submit(solve)
        try:
            return future.result(timeout=budget_seconds + self._grace_seconds)
        except TimeoutError:
            if future.done():
                raise  # the solver itself raised TimeoutError; not a wall-clock timeout
            future.cancel()
            log.warning("planning solve passed its %.1f s budget plus grace", budget_seconds)
            raise PlanningTimeout("The solver did not finish within its time budget") from None

    def close(self) -> None:
        """Stop accepting solves; queued ones are cancelled, running ones are not waited for."""
        self._pool.shutdown(wait=False, cancel_futures=True)
