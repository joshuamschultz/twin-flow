"""Idempotency claims and input checks for the planning service (COMP-117, D-120, REQ-167).

A claim key names one caller's request key. The caller id is percent-escaped (`%` and `:`)
before the `:` separator, so the first `:` always ends the caller id and two different
`(caller_id, request_key)` pairs never encode to the same key: `("alice:x", "k")` becomes
`alice%3Ax:k`, `("alice", "x:k")` becomes `alice:x:k`. A plain caller id such as `arc`
encodes unchanged, so planning claims share one namespace with today's job claims
(`Workspace.submit_schedule_baseline` keys).

The input digest is the SHA-256 of the canonical JSON of the method name and its inputs, so
one key reused by a different method is a conflict, not a replay.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

MAX_KEY_LENGTH = 200
MAX_SEED = 2**32 - 1


def claim_key(caller_id: str, request_key: str) -> str:
    """The unambiguous `requests` key for one caller's request key; refuses bad ids."""
    _check_text("caller_id", caller_id)
    _check_text("request_key", request_key)
    escaped = caller_id.replace("%", "%25").replace(":", "%3A")
    return f"{escaped}:{request_key}"


def input_digest(method: str, inputs: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of `method` and `inputs`."""
    canonical = json.dumps(
        {"method": method, "inputs": inputs},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def check_budget(requested: float | None) -> None:
    """A requested time budget must be a finite number of seconds above zero (REQ-169)."""
    if requested is not None and not (math.isfinite(requested) and requested > 0):
        raise ValueError("time_budget_seconds must be finite and greater than 0")


def check_seed(seed: int) -> None:
    """Seeds are unsigned 32-bit integers, as for every other workspace solve."""
    if not 0 <= seed <= MAX_SEED:
        raise ValueError("seed must be between 0 and 2**32 - 1")


def _check_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")
    if len(value) > MAX_KEY_LENGTH:
        raise ValueError(f"{name} is longer than {MAX_KEY_LENGTH} characters")
    if not value.isprintable():
        raise ValueError(f"{name} contains control characters")


@dataclass
class _Entry:
    lock: threading.Lock = field(default_factory=threading.Lock)
    holders: int = 0


class KeyLocks:
    """One lock per claim key, kept only while someone holds or waits for it.

    Concurrent duplicates of one request in this process serialize here, so exactly one
    solve runs and the others then find its stored claim.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @contextmanager
    def hold(self, key: str) -> Iterator[None]:
        """Hold the lock for `key` for the duration of the block."""
        with self._guard:
            entry = self._entries.setdefault(key, _Entry())
            entry.holders += 1
        try:
            with entry.lock:
                yield
        finally:
            with self._guard:
                entry.holders -= 1
                if entry.holders == 0:
                    del self._entries[key]
