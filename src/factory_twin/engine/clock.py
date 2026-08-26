"""COMP-001 RunContext — owns the SimPy environment and one replication's lifetime."""

from __future__ import annotations

import simpy


class RunContext:
    """Clock, run id, horizon, teardown; the run-scoped registry handed to Locations."""

    def __init__(self, run_id: str, seed: int, horizon: float) -> None:
        self.run_id = run_id
        self.seed = seed
        self.horizon = horizon
        self.env = simpy.Environment()
