"""Sources that misbehave only inside a spawned pool worker (evaluator pool tests).

They live in their own module so a spawn child can import them by name. In the parent
process they act as the seed-calibration profile source, so the deterministic analyses and
any in-process work stay normal.
"""

from __future__ import annotations

import multiprocessing
import os
import time

from twinflow.execution.policy import Distribution, FactorRule, LogNormal, SimulationPolicy
from twinflow.execution.sources import OpContext, ProfileSource

POLICY = SimulationPolicy(
    distribution_source="profile",
    run_factors=(FactorRule(distribution=LogNormal(mean=1.0, cv=0.15)),),
)


def _in_worker() -> bool:
    return multiprocessing.parent_process() is not None


class StallingSource(ProfileSource):
    """Every run-factor lookup in a worker sleeps `STALL_SECONDS` first."""

    STALL_SECONDS = 60.0

    def run_factor(self, ctx: OpContext) -> Distribution:
        if _in_worker():
            time.sleep(self.STALL_SECONDS)
        return super().run_factor(ctx)


class DyingSource(ProfileSource):
    """A worker that looks up a run factor dies at once, as an OOM kill would."""

    def run_factor(self, ctx: OpContext) -> Distribution:
        if _in_worker():
            os._exit(1)
        return super().run_factor(ctx)
