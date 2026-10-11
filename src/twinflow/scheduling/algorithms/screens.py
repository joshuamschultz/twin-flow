"""Moore-Hodgson on-time screen on one work center (COMP-011, REQ-096; Pinedo Alg 3.3.1, p.47-48).

An analysis, not a solver: `on_time_screen(compiled, work_center_id=None)` tells which jobs
can be on time at a center if it ran them alone, and which cannot. It registers through
`ANALYSES` (`registry.analysis("on_time_screen")`) and exports `SOLVERS = ()`.

Each job with a pending op at the center is one single-machine job:

- p = the op's shortest eligible run / m, m = the center's machine count (the center's
  capacity spread evenly over its machines);
- due = the op's local due `dloc` (`ProblemIndex.local_due`, phi = 2: due minus twice the
  job's later shortest runs); a job without a due date is never late.

A job with several pending ops at the center sums their p and takes the last op's dloc
[mine]. In-progress ops are not pending (DEV-9) and are left out. Jobs go in EDD order of
dloc (ties: problem order); when a job's completion exceeds its dloc, the longest job in the
on-time set so far (ties: the later one in EDD order) moves to the late set.

`None` picks the bottleneck: the center with the highest load ratio, the sum of its pending
ops' shortest runs over its machine count (ties: center id); centers without machines are
skipped. A leaf of `algorithms/` (D-069): it imports no other algorithm module.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from twinflow.scheduling.kernel.index import OpData, ProblemIndex
from twinflow.scheduling.model import CompiledProblem
from twinflow.scheduling.registry import AnalysisSpec, SolverSpec

NAME = "on_time_screen"


@dataclass(frozen=True, slots=True)
class OnTimeScreen:
    """The screen's verdict on one center; both tuples hold job ids in EDD (dloc) order."""

    work_center_id: str
    on_time: tuple[str, ...]
    late: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ScreenJob:
    job_id: str
    order: int
    processing: float
    due: float


def on_time_screen(compiled: CompiledProblem, work_center_id: str | None = None) -> OnTimeScreen:
    """Moore-Hodgson on `work_center_id`, or on the bottleneck when None (module docstring).

    ValueError for an unknown center, or when no center has a machine.
    """
    index = ProblemIndex.build(compiled.problem)
    center = bottleneck(index) if work_center_id is None else work_center_id
    if center not in index.center_machines:
        raise ValueError(f"unknown work center {center!r}")
    jobs = _screen_jobs(index, center)
    on_time = _moore_hodgson(jobs)
    late = tuple(job.job_id for job in jobs if job.job_id not in on_time)
    kept = tuple(job.job_id for job in jobs if job.job_id in on_time)
    return OnTimeScreen(work_center_id=center, on_time=kept, late=late)


def bottleneck(index: ProblemIndex) -> str:
    """The center with the highest load ratio (ties: center id); ValueError when none."""
    ratios = {
        center: _load(index, center) / len(machines)
        for center, machines in index.center_machines.items()
        if machines
    }
    if not ratios:
        raise ValueError("no work center has a machine")
    return min(ratios, key=lambda center: (-ratios[center], center))


def _pending_at(index: ProblemIndex, center: str) -> list[OpData]:
    return [op for op in index.ops if op.work_center_id == center and op.in_progress is None]


def _shortest_run(op: OpData) -> float:
    return min(option.run for option in op.options)


def _load(index: ProblemIndex, center: str) -> float:
    return math.fsum(_shortest_run(op) for op in _pending_at(index, center))


def _screen_jobs(index: ProblemIndex, center: str) -> list[_ScreenJob]:
    """One single-machine job per job with pending ops at `center`, in EDD order."""
    m = len(index.machines_of(center))
    if m == 0:
        raise ValueError(f"work center {center!r} has no machine")
    processing: dict[int, float] = {}
    due: dict[int, float] = {}
    for op in _pending_at(index, center):  # route order within a job: the last op wins
        processing[op.job_pos] = processing.get(op.job_pos, 0.0) + _shortest_run(op) / m
        due[op.job_pos] = math.inf if op.local_due is None else op.local_due
    jobs = [
        _ScreenJob(index.jobs[job_pos].id, job_pos, processing[job_pos], due[job_pos])
        for job_pos in processing
    ]
    return sorted(jobs, key=lambda job: (job.due, job.order))


def _moore_hodgson(jobs: list[_ScreenJob]) -> set[str]:
    """Job ids of the on-time set (Alg 3.3.1); `jobs` in EDD order."""
    longest: list[tuple[float, int, str]] = []  # max-heap by (p, EDD rank)
    completion = 0.0
    for rank, job in enumerate(jobs):
        heapq.heappush(longest, (-job.processing, -rank, job.job_id))
        completion += job.processing
        if completion > job.due:
            negative_p, _, _ = heapq.heappop(longest)
            completion += negative_p
    return {job_id for _, _, job_id in longest}


SOLVERS: tuple[SolverSpec, ...] = ()
"""No solver: the screen is an analysis (COMP-011)."""

ANALYSES = (AnalysisSpec(name=NAME, function=on_time_screen),)
"""Registry entries (DEV-23 shape)."""
