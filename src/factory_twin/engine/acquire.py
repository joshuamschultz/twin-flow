"""COMP-002 ResourceAcquirer — the deadlock guard.

Acquires machine then operator in one fixed global order and manually releases any leg
already held when a wait is abandoned. Every dual-resource path routes through here.
"""

from __future__ import annotations


class ResourceAcquirer:
    """The single code path for a machine+operator dual acquire (D-... deadlock guard)."""

    def __init__(self) -> None:
        raise NotImplementedError("T-005")
