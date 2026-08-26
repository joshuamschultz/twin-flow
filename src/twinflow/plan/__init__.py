"""Layer 3 — work orders, release timing, initial WIP; the in-process run entry point."""

from __future__ import annotations


def load_plan(path: str) -> list[object]:
    """Public API: read a plan .xlsx/.csv into WorkOrders."""
    raise NotImplementedError("T-031")


def run(model: object, plan: list[object], seed: int, replication_index: int) -> object:
    """Public API: execute one terminating replication in-process."""
    raise NotImplementedError("T-033")
