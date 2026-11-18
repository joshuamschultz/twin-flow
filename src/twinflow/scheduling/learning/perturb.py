"""Perturbation runs and the pure K-table update (T-164, COMP-025, REQ-122, D-082).

Pinedo Example 18.2.1 (p.488-489), SDD "K learning" pseudo-code:

- `perturbation_runs(request, center_id, table, delta)`: ATCS (`registry.get("atcs", ...)`
  by name at call time) runs once with the table (`as_k_lookup`); that run's record for the
  center is the base, (K1, K2) = `lookup(table, stats)`, source `table`. Then 8 more runs put
  the center at its neighbors K1 +- delta K1, K2 +- delta K2 and the 4 corners (clamped);
  every other center keeps its table K, so a run's primary value measures this center's K.
  Each record carries the center's statistics and `objective_value` = that run's primary
  value under the request's objective; the neighbors are relabelled source `perturbed`.
- `update_k_table(table, stats, best, runs, threshold, radius)` (pure): WHEN the base value
  is positive and `best <= (1 - threshold) * base`, the nearest entry in (tau, R, eta,
  log2 m) within `radius` takes best's K (its `runs` + 1), or a new entry is added at the
  stats point; otherwise the input table is returned. A relative threshold has no meaning
  for a base value of 0 or below, so those never update [mine].

Default radius 0.05 [tuned]: chosen on the TRAINING seeds only. Seeds 0-9 trained (ten
rounds from the seed table, objective as the bench), seeds 10-19 validated (mean ATCS bench
score per round). Worst round vs the seed table, radius 0.02 / 0.05 / 0.1 / 0.15: -1.7 /
-3.5 / -2.5 / -0.1 percent. The default is the radius with the widest worst-round margin,
since the gate is "never worse". A larger radius lets one center's K overwrite a seed entry
that many other centers interpolate from.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Literal

from twinflow.scheduling import registry
from twinflow.scheduling.kernel.stats import clamp_k
from twinflow.scheduling.learning.ktable import (
    KEntry,
    KTable,
    StatsPoint,
    TableLookup,
    as_k_lookup,
    point_of,
)
from twinflow.scheduling.model import AtcsRun, SolveRequest
from twinflow.scheduling.solver import solve

ATCS = "atcs"
DEFAULT_DELTA = 0.25
DEFAULT_THRESHOLD = 0.02
DEFAULT_RADIUS = 0.05
_STEPS = tuple(step for step in itertools.product((-1, 0, 1), repeat=2) if step != (0, 0))


class CenterNotScheduledError(ValueError):
    """The center has no ATCS run record (unknown id, or no machines)."""


@dataclass(frozen=True, slots=True)
class _CenterOverride:
    """Table K everywhere except at one statistics point, where K is fixed; picklable."""

    table: TableLookup
    center: tuple[float, float, float, int]
    k: tuple[float, float]

    def __call__(self, tau: float, R: float, eta: float, m: int) -> tuple[float, float]:  # noqa: N803
        if (tau, R, eta, m) == self.center:
            return self.k
        return self.table(tau, R, eta, m)


def perturbation_runs(
    request: SolveRequest, center_id: str, table: KTable, delta: float = DEFAULT_DELTA
) -> tuple[AtcsRun, ...]:
    """The base run at the table K, then the 8 neighbor runs (9 `AtcsRun` records)."""
    table_lookup = as_k_lookup(table)
    base = _center_run(request, center_id, table_lookup, "table")
    center = (base.tau, base.R, base.eta, base.m)
    neighbors = tuple(
        _center_run(
            request,
            center_id,
            _CenterOverride(table_lookup, center, _neighbor_k(base, a, b, delta)),
            "perturbed",
        )
        for a, b in _STEPS
    )
    return (base, *neighbors)


def _neighbor_k(base: AtcsRun, a: int, b: int, delta: float) -> tuple[float, float]:
    return clamp_k(base.K1 * (1.0 + a * delta), base.K2 * (1.0 + b * delta))


def _center_run(
    request: SolveRequest,
    center_id: str,
    k_lookup: TableLookup | _CenterOverride,
    source: Literal["table", "perturbed"],
) -> AtcsRun:
    result = solve(request, solver=registry.get(ATCS, k_lookup=k_lookup))
    run = next((r for r in result.schedule.atcs_runs if r.work_center_id == center_id), None)
    if run is None:
        raise CenterNotScheduledError(f"no ATCS run for work center {center_id!r}")
    value = result.schedule.primary_value
    return run.model_copy(update={"source": source, "objective_value": value})


def update_k_table(
    table: KTable,
    stats: StatsPoint,
    best: AtcsRun,
    runs: tuple[AtcsRun, ...],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    radius: float = DEFAULT_RADIUS,
) -> KTable:
    """A new table with best's K adopted near `stats` when it improves enough; else `table`."""
    if not runs:
        raise ValueError("runs must hold the base run first")
    if not _improves(runs[0].objective_value, best.objective_value, threshold):
        return table
    query = point_of(stats)
    nearest = min(
        range(len(table.entries)),
        key=lambda i: math.dist(point_of(table.entries[i]), query),
        default=None,
    )
    entries = list(table.entries)
    if nearest is not None and math.dist(point_of(entries[nearest]), query) <= radius:
        moved = entries[nearest]
        entries[nearest] = moved.model_copy(
            update={"K1": best.K1, "K2": best.K2, "runs": moved.runs + 1}
        )
    else:
        entries.append(
            KEntry(
                tau=stats.tau, R=stats.R, eta=stats.eta, m=stats.m, K1=best.K1, K2=best.K2, runs=1
            )
        )
    return table.model_copy(update={"entries": tuple(entries)})


def _improves(base: float | None, best: float | None, threshold: float) -> bool:
    if base is None or best is None or base <= 0:
        return False
    return best <= (1.0 - threshold) * base
