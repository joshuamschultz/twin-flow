"""COMP-121 per-caller limits: a call-rate token bucket, solve slots, and a result-size guard.

State is in memory and per process; one process serves one deployment (D-116). Memory is
one small record per caller id, and caller ids come from the token registry, so it is
bounded by the registry size.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel

DEFAULT_RESULT_LIMIT_BYTES = 1_048_576
# A solve's end time is unknown when a slot is refused; one second is a polite retry hint.
SOLVE_RETRY_AFTER_SECONDS = 1.0


class RateLimited(Exception):  # noqa: N818 - the name is the pinned contract (COMP-121)
    """A caller exceeded its call rate or its concurrent-solve slots."""

    code: ClassVar[str] = "rate_limited"

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(f"rate limited; retry after {retry_after_seconds:.3f} s")
        self.retry_after_seconds = retry_after_seconds


class ResultTooLarge(Exception):  # noqa: N818 - the name is the pinned contract (COMP-121)
    """A serialized result is larger than its limit. The message never carries content."""

    code: ClassVar[str] = "size_limit"


@dataclass
class _Bucket:
    tokens: float
    updated: float


class CallerLimiter:
    """Thread-safe per-caller token bucket and per-caller solve semaphore.

    The bucket starts full at ``burst`` and refills at ``calls_per_minute / 60`` tokens
    per second, capped at ``burst``. A refused call consumes nothing.
    """

    def __init__(
        self,
        *,
        calls_per_minute: int = 120,
        burst: int = 20,
        max_concurrent_solves_per_caller: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        for name, value in (
            ("calls_per_minute", calls_per_minute),
            ("burst", burst),
            ("max_concurrent_solves_per_caller", max_concurrent_solves_per_caller),
        ):
            if value < 1:
                raise ValueError(f"{name} must be positive")
        self._rate = calls_per_minute / 60.0
        self._burst = float(burst)
        self._max_solves = max_concurrent_solves_per_caller
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}
        self._solves: dict[str, int] = {}

    def check_call(self, caller_id: str) -> None:
        """Spend one call token for ``caller_id`` or raise ``RateLimited``."""
        with self._lock:
            now = self._clock()
            bucket = self._buckets.setdefault(caller_id, _Bucket(self._burst, now))
            bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated) * self._rate)
            bucket.updated = now
            if bucket.tokens < 1.0:
                raise RateLimited((1.0 - bucket.tokens) / self._rate)
            bucket.tokens -= 1.0

    @contextmanager
    def solve_slot(self, caller_id: str) -> Iterator[None]:
        """Hold one of the caller's solve slots for the ``with`` body, or raise ``RateLimited``."""
        with self._lock:
            active = self._solves.get(caller_id, 0)
            if active >= self._max_solves:
                raise RateLimited(SOLVE_RETRY_AFTER_SECONDS)
            self._solves[caller_id] = active + 1
        try:
            yield
        finally:
            with self._lock:
                remaining = self._solves[caller_id] - 1
                if remaining:
                    self._solves[caller_id] = remaining
                else:
                    del self._solves[caller_id]


def ensure_result_size(result: BaseModel | bytes, limit: int = DEFAULT_RESULT_LIMIT_BYTES) -> bytes:
    """The result as JSON bytes, or ``ResultTooLarge`` when longer than ``limit`` bytes."""
    payload = result if isinstance(result, bytes) else result.model_dump_json().encode("utf-8")
    if len(payload) > limit:
        raise ResultTooLarge(f"result is larger than {limit} bytes")
    return payload
