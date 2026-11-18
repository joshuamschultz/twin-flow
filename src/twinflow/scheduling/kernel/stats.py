"""Instance statistics of one work center for ATCS (COMP-003, REQ-093, SDD ATCS pseudo-code).

`InstanceStats.for_center(index, center_id, now)` (Pinedo p.375-378, p.389):

- P = pending ops (not in progress) with an eligible option on a machine of the center;
  m = the center's machine count;
- pbar = mean over P of the mean run of its options in the center;
- sbar = mean of s_i(l, o) over machines i of the center, l in P_i plus the machine state,
  o in P_i, l != o, where P_i is the ops of P eligible on i (a setup on i only happens
  between ops that can both run there);
- Cmax_hat = (sum over P of shortest run in the center + |P| * sbar) / m;
- tau = 1 - mean(dloc(o) - now) / Cmax_hat; R = (max dloc - min dloc) / Cmax_hat over the
  ops of P with a local due (0 when none has one or Cmax_hat is 0); eta = sbar / pbar.

Pair sampling [mine, SDD]: the pair list is sorted by (machine id, state first then
previous op id, next op id). With more than `MAX_SETUP_PAIRS` pairs, every `stride`-th pair
is used, stride = ceil(total / MAX_SETUP_PAIRS), starting at the first. The k-th pair is
computed arithmetically, so the list is never built (it is quadratic in |P|) and the result
does not depend on input order.

`atcs_k(tau, R, eta)` is the p.376 K formula with its clamps (`K1_RANGE`, `K2_RANGE`); it
lives here, beside the statistics it reads, so `algorithms/atcs.py` and the K-table seed in
`learning/` share one copy without one importing the other (D-069, D-082). `KLookup` is the
type of the K-table callable ATCS takes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from twinflow.scheduling.kernel.index import OpData, ProblemIndex

MAX_SETUP_PAIRS = 20_000
K1_RANGE = (0.5, 10.0)
K2_RANGE = (0.05, 10.0)

KLookup = Callable[[float, float, float, int], tuple[float, float]]
"""`k_lookup(tau, R, eta, m) -> (K1, K2)`; picklable so it survives the process pool.

Here so ATCS and the portfolio runner (which passes it to every `atcs` stage) share it."""


def atcs_k(tau: float, R: float, eta: float) -> tuple[float, float]:  # noqa: N803
    """(K1, K2) by Pinedo p.376, clamped; K2 is finite for eta 0 (setup factor 1 there)."""
    k1 = 4.5 + R if R <= 0.5 else 6.0 - 2.0 * R
    k2 = tau / (2.0 * math.sqrt(eta)) if eta > 0 else K2_RANGE[1]
    return clamp_k(k1, k2)


def clamp_k(k1: float, k2: float) -> tuple[float, float]:
    """K1 into `K1_RANGE`, K2 into `K2_RANGE`."""
    return min(max(k1, K1_RANGE[0]), K1_RANGE[1]), min(max(k2, K2_RANGE[0]), K2_RANGE[1])


@dataclass(frozen=True, slots=True, kw_only=True)
class InstanceStats:
    """ATCS factors of one work center (names follow Pinedo: tau, R, eta)."""

    work_center_id: str
    m: int
    pbar: float
    sbar: float
    cmax_hat: float
    tau: float
    R: float
    eta: float
    sampled_pairs: int

    @classmethod
    def for_center(cls, index: ProblemIndex, center_id: str, now: float) -> InstanceStats:
        machines = index.machines_of(center_id)
        in_center = set(machines)
        runs = _center_runs(index, in_center)
        m = len(machines)
        pbar = _mean([sum(r) / len(r) for r in runs.values()])
        sbar, sampled = _mean_setup(index, machines, runs)
        shortest = sum(min(r) for r in runs.values())
        cmax_hat = (shortest + len(runs) * sbar) / m if m else 0.0
        local_dues = [d for p in runs if (d := index.ops[p].local_due) is not None]
        tau, spread = _due_factors(local_dues, now, cmax_hat)
        return cls(
            work_center_id=center_id,
            m=m,
            pbar=pbar,
            sbar=sbar,
            cmax_hat=cmax_hat,
            tau=tau,
            R=spread,
            eta=sbar / pbar if pbar > 0 else 0.0,
            sampled_pairs=sampled,
        )


def _is_pending(op_data: OpData) -> bool:
    return op_data.in_progress is None


def _center_runs(index: ProblemIndex, machines: set[str]) -> dict[int, list[float]]:
    """P as op position -> runs of its eligible options on the center's machines."""
    runs: dict[int, list[float]] = {}
    for p, op_data in enumerate(index.ops):
        if not _is_pending(op_data):
            continue
        in_center = [o.run for o in op_data.options if o.machine_id in machines]
        if in_center:
            runs[p] = in_center
    return runs


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _due_factors(local_dues: list[float], now: float, cmax_hat: float) -> tuple[float, float]:
    """(tau, R) from the local dues; (0, 0) without dues or without work."""
    if not local_dues or cmax_hat <= 0:
        return 0.0, 0.0
    tau = 1.0 - _mean([d - now for d in local_dues]) / cmax_hat
    return tau, (max(local_dues) - min(local_dues)) / cmax_hat


def _eligible_ids(index: ProblemIndex, runs: dict[int, list[float]]) -> dict[str, list[str]]:
    """Per machine, the sorted ids of the ops of P eligible on it."""
    by_machine: dict[str, list[str]] = {}
    for p in runs:
        op_data = index.ops[p]
        for option in op_data.options:
            by_machine.setdefault(option.machine_id, []).append(op_data.id)
    return {machine: sorted(ids) for machine, ids in by_machine.items()}


def _pair_count(n: int) -> int:
    """Pairs on one machine with n eligible ops: n from the state, n * (n - 1) between ops."""
    return n * n


def _pair_at(ids: list[str], k: int) -> tuple[str | None, str]:
    """The k-th (previous, next) pair of one machine in sorted order; previous None = state."""
    n = len(ids)
    if k < n:
        return None, ids[k]
    prev, rest = divmod(k - n, n - 1)
    return ids[prev], ids[rest if rest < prev else rest + 1]


def _mean_setup(
    index: ProblemIndex, machines: tuple[str, ...], runs: dict[int, list[float]]
) -> tuple[float, int]:
    """(sbar, sampled pair count) over the stride sample of the sorted pair list."""
    eligible = _eligible_ids(index, runs)
    blocks = [(m, eligible[m]) for m in sorted(machines) if m in eligible]
    total = sum(_pair_count(len(ids)) for _, ids in blocks)
    if total == 0:
        return 0.0, 0
    stride = math.ceil(total / MAX_SETUP_PAIRS)
    seconds = 0.0
    sampled = 0
    offset = 0  # index into the sorted pair list where the current machine's block starts
    for machine_id, ids in blocks:
        size = _pair_count(len(ids))
        for k in range(-offset % stride, size, stride):
            prev, nxt = _pair_at(ids, k)
            seconds += index.setups.seconds(machine_id, prev, nxt)
            sampled += 1
        offset += size
    return seconds / sampled, sampled
