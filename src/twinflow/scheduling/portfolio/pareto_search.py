"""Pareto local search from dispatch seeds (COMP-022, REQ-117, D-078, Pinedo p.418-420).

`pareto_search(index, seeds, axes, deadline, *, depth=2)` keeps an archive of mutually
non-dominated plans over the signed axis values (`objectives.signed`):

1. Each seed `(choice, sequences)` is decoded and offered to the archive (always, even past
   the deadline, so a passed deadline returns the seeds' own non-dominated points).
2. Level by level up to `depth`: every archive member first reached at the previous level
   is expanded by its swap neighborhood, every adjacent pair on every machine sequence
   (COMP-017 `apply(Move.swap)`; ops of one job and in-progress ops are never swapped).
   Each new feasible neighbor is decoded, scored with `kernel.score.fast_values` (no
   previous plan) and offered to the archive. A level's frontier is fixed after the whole
   level, so the result does not depend on seed order.
3. The search stops when `time.monotonic()` passes `deadline`.

Ties: two plans with equal signed values on every axis keep the one with the smaller plan
key (sorted machine sequences), so the archive is deterministic. Output order: signed axis
values, then plan key.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.moves import Move, apply
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.model import ObjectiveKind
from twinflow.scheduling.objectives import signed

Choice = Mapping[str, str]
Sequences = Mapping[str, Sequence[str]]
Seed = tuple[Choice, Sequences]
PlanKey = tuple[tuple[str, tuple[str, ...]], ...]
Signed = tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ArchivePoint:
    """A plan in the archive: machine choice, sequences and its objective values."""

    choice: Mapping[str, str]
    sequences: Mapping[str, tuple[str, ...]]
    values: Mapping[ObjectiveKind, float]


def pareto_search(
    index: ProblemIndex,
    seeds: Sequence[Seed],
    axes: Sequence[ObjectiveKind],
    deadline: float,
    *,
    depth: int = 2,
) -> tuple[ArchivePoint, ...]:
    """The non-dominated archive reached within `depth` swaps of the seeds (module docstring)."""
    archive = _Archive(axes)
    seen: set[PlanKey] = set()
    frontier: list[PlanKey] = []
    for choice, sequences in seeds:
        point = _evaluate(index, choice, sequences, seen)
        if point is not None:
            archive.offer(point)
            frontier.append(_plan_key(point.sequences))
    for _ in range(depth):
        level_starts = [archive.get(key) for key in sorted(set(frontier))]
        frontier = []
        for start in level_starts:
            if start is None:
                continue
            for choice, sequences in _swap_neighbors(index, start):
                if time.monotonic() >= deadline:
                    return archive.points()
                point = _evaluate(index, choice, sequences, seen)
                if point is not None and archive.offer(point):
                    frontier.append(_plan_key(point.sequences))
    return archive.points()


def _plan_key(sequences: Mapping[str, Sequence[str]]) -> PlanKey:
    return tuple(sorted((machine, tuple(seq)) for machine, seq in sequences.items()))


def _evaluate(
    index: ProblemIndex, choice: Choice, sequences: Sequences, seen: set[PlanKey]
) -> ArchivePoint | None:
    """The plan as an archive point; None when already seen or when it does not decode."""
    key = _plan_key(sequences)
    if key in seen:
        return None
    seen.add(key)
    decoded = decode(index, choice, sequences)
    if not isinstance(decoded, Decoded):
        return None
    return ArchivePoint(
        choice=dict(choice),
        sequences={machine: tuple(seq) for machine, seq in sequences.items()},
        values=fast_values(index, decoded, None, 0.0),
    )


def _swap_neighbors(
    index: ProblemIndex, point: ArchivePoint
) -> Iterator[tuple[dict[str, str], dict[str, list[str]]]]:
    """Plans one adjacent same-machine swap away, machines and positions in sorted order."""
    for machine in sorted(point.sequences):
        for u, v in pairwise(point.sequences[machine]):
            if _swappable(index, u, v):
                yield apply(Move.swap(u, v), point.choice, point.sequences)


def _swappable(index: ProblemIndex, u: str, v: str) -> bool:
    first, second = index.op(u), index.op(v)
    in_progress = first.in_progress is not None or second.in_progress is not None
    return first.job_pos != second.job_pos and not in_progress


class _Archive:
    """Mutually non-dominated points keyed by plan key (equal values: smaller key kept)."""

    def __init__(self, axes: Sequence[ObjectiveKind]) -> None:
        self._axes = tuple(axes)
        self._members: dict[PlanKey, tuple[Signed, ArchivePoint]] = {}

    def _signed(self, point: ArchivePoint) -> Signed:
        return tuple(signed(kind, point.values[kind]) for kind in self._axes)

    def offer(self, point: ArchivePoint) -> bool:
        """Add `point` unless a member weakly dominates it; drop the members it dominates."""
        key = _plan_key(point.sequences)
        values = self._signed(point)
        for other_key, (other, _) in self._members.items():
            if _weakly_dominates(other, values) and (other != values or other_key < key):
                return False
        self._members = {
            k: member
            for k, member in self._members.items()
            if not _weakly_dominates(values, member[0])
        }
        self._members[key] = (values, point)
        return True

    def get(self, key: PlanKey) -> ArchivePoint | None:
        member = self._members.get(key)
        return None if member is None else member[1]

    def points(self) -> tuple[ArchivePoint, ...]:
        ordered = sorted(self._members.items(), key=lambda item: (item[1][0], item[0]))
        return tuple(point for _, (_, point) in ordered)


def _weakly_dominates(a: Signed, b: Signed) -> bool:
    return all(x <= y for x, y in zip(a, b, strict=True))
