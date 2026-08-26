"""COMP-001 RunContext — owns the SimPy environment and one replication's lifetime."""

from __future__ import annotations


class RunContext:
    """Clock, run id, horizon, teardown; the run-scoped registry handed to Locations."""

    def __init__(self) -> None:
        raise NotImplementedError("T-005")
