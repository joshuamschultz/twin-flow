"""Pareto local search from dispatch seeds (T-159; COMP-022, REQ-117, D-078, Pinedo p.418-420).

API these tests pin (`twinflow.scheduling.portfolio.pareto_search`):

- `pareto_search(index, seeds, axes, deadline, *, depth=2) -> tuple[ArchivePoint, ...]`.
  `index` is a `kernel.index.ProblemIndex`; `seeds` a sequence of `(choice, sequences)` pairs
  (`choice` op id -> machine id, `sequences` machine id -> op ids, the moves API shape);
  `axes` 2 or 3 objective kinds; `deadline` a `time.monotonic()` instant (a passed deadline
  returns at most the seeds' own non-dominated points); `depth` the most swaps from a seed.
- `ArchivePoint(choice, sequences, values)`: frozen dataclass; `values` maps at least the axes
  to the decoded plan's values (`kernel.score.fast_values`, no previous plan).
- The archive is non-dominated over the signed axis values; every evaluated neighbor (adjacent
  same-machine swaps on a single-machine problem) is offered to it; deterministic.

Seeds are the dispatch schedules of fixture 14.2.1 given explicitly (book sequences, p.420),
so these tests do not depend on the dispatch rules' implementation.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import Any

import pytest
from kernel_cases import compile_request, load_request, machine, only_choice, op, request_from

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import CompiledProblem, ObjectiveKind
from twinflow.scheduling.objectives import evaluate, signed
from twinflow.scheduling.verifier import verify

WT = ObjectiveKind.WEIGHTED_TARDINESS
LMAX = ObjectiveKind.MAX_LATENESS
AXES = (WT, LMAX)

EDD = "1,3,4,2"
SPT = "2,4,1,3"
WSPT = "4,2,1,3"
ATCS = "2,4,3,1"

PINEDO_FRONT = {
    "2,4,3,1": (98.0, 36.0),
    "1,4,2,3": (101.0, 32.0),
    "2,4,1,3": (110.0, 30.0),
    "1,4,3,2": (129.0, 20.0),
    "1,3,4,2": (177.0, 19.0),
}

Seed = tuple[Mapping[str, str], Mapping[str, Sequence[str]]]


def _search() -> ModuleType:
    return importlib.import_module("twinflow.scheduling.portfolio.pareto_search")


def _ops(order: str) -> list[str]:
    return [f"{job}-1" for job in order.split(",")]


def _label(point: Any) -> str:
    return ",".join(op_id.split("-")[0] for op_id in point.sequences["M1"])


@pytest.fixture(scope="module")
def fixture_14_2_1() -> tuple[CompiledProblem, ProblemIndex, dict[str, str]]:
    request = load_request("pinedo_14_2_1")
    compiled = compile_request(request)
    return compiled, ProblemIndex.build(compiled.problem), only_choice(request)


def _seeds(choice: Mapping[str, str], orders: Sequence[str]) -> list[Seed]:
    return [(dict(choice), {"M1": _ops(order)}) for order in orders]


def _run(
    fixture: tuple[CompiledProblem, ProblemIndex, dict[str, str]],
    orders: Sequence[str] = (EDD, SPT, WSPT, ATCS),
    *,
    budget: float = 30.0,
    depth: int = 2,
) -> tuple[Any, ...]:
    _, index, choice = fixture
    deadline = time.monotonic() + budget
    result: tuple[Any, ...] = _search().pareto_search(
        index, _seeds(choice, orders), AXES, deadline, depth=depth
    )
    return result


def _dominates(a: Mapping[ObjectiveKind, float], b: Mapping[ObjectiveKind, float]) -> bool:
    sa = [signed(k, a[k]) for k in AXES]
    sb = [signed(k, b[k]) for k in AXES]
    return all(x <= y for x, y in zip(sa, sb, strict=True)) and sa != sb


def test_archive_holds_all_five_pareto_schedules_within_depth_two(fixture_14_2_1: Any) -> None:
    archive = _run(fixture_14_2_1)
    found = {_label(p): (p.values[WT], p.values[LMAX]) for p in archive}
    assert found == PINEDO_FRONT
    assert len(archive) == len(PINEDO_FRONT)


def test_depth_one_misses_1423_which_is_two_swaps_from_every_seed(fixture_14_2_1: Any) -> None:
    archive = _run(fixture_14_2_1, depth=1)
    labels = {_label(p) for p in archive}
    assert "1,4,2,3" not in labels
    assert {"2,4,3,1", "2,4,1,3", "1,4,3,2", "1,3,4,2"} <= labels


def test_archive_is_mutually_non_dominated(fixture_14_2_1: Any) -> None:
    archive = _run(fixture_14_2_1, depth=1)
    for a in archive:
        for b in archive:
            assert not _dominates(a.values, b.values)


def test_archive_points_decode_and_verify_with_their_reported_values(
    fixture_14_2_1: Any,
) -> None:
    compiled, index, _ = fixture_14_2_1
    for point in _run(fixture_14_2_1):
        decoded = decode(index, point.choice, point.sequences)
        assert isinstance(decoded, Decoded), decoded
        schedule = decoded.schedule("pareto")
        verdict = verify(compiled, (), schedule)
        assert verdict.valid, verdict.issues
        truth = evaluate(compiled, schedule)
        for kind in AXES:
            assert point.values[kind] == pytest.approx(truth[kind])


def test_archive_point_is_frozen(fixture_14_2_1: Any) -> None:
    point = _run(fixture_14_2_1)[0]
    with pytest.raises(AttributeError):
        point.values = {}


def test_search_is_deterministic(fixture_14_2_1: Any) -> None:
    def snapshot() -> list[tuple[str, tuple[float, float]]]:
        return [(_label(p), (p.values[WT], p.values[LMAX])) for p in _run(fixture_14_2_1)]

    assert snapshot() == snapshot()


def test_seed_order_does_not_change_the_archive_set(fixture_14_2_1: Any) -> None:
    forward = {_label(p) for p in _run(fixture_14_2_1, (EDD, SPT, WSPT, ATCS))}
    backward = {_label(p) for p in _run(fixture_14_2_1, (ATCS, WSPT, SPT, EDD))}
    assert forward == backward == set(PINEDO_FRONT)


def test_passed_deadline_returns_only_seed_points(fixture_14_2_1: Any) -> None:
    archive = _run(fixture_14_2_1, budget=-1.0)
    labels = {_label(p) for p in archive}
    assert labels <= {EDD, SPT, WSPT, ATCS}
    assert WSPT not in labels  # (118, 32) is dominated by SPT's (110, 30)


def _long_single_machine() -> tuple[ProblemIndex, list[Seed]]:
    """200 one-op jobs on one machine: depth 2 means ~40,000 decodes, far beyond 1 s."""
    jobs = [
        {
            "id": f"j{i:03d}",
            "due": float((37 * i) % 900),
            "operations": [op(f"o{i:03d}", {"M": float(3 + i % 7)})],
        }
        for i in range(200)
    ]
    problem = {
        "id": "long",
        "jobs": jobs,
        "machines": [machine("M")],
        "work_centers": [{"id": "WC"}],
    }
    request = request_from(problem)
    index = ProblemIndex.build(compile_request(request).problem)
    choice = {f"o{i:03d}": "M" for i in range(200)}
    by_id = [f"o{i:03d}" for i in range(200)]
    by_due = sorted(by_id, key=lambda o: ((37 * int(o[1:])) % 900, o))
    return index, [(choice, {"M": by_id}), (choice, {"M": by_due})]


def test_search_stops_at_its_deadline_on_a_long_instance() -> None:
    index, seeds = _long_single_machine()
    budget = 0.5
    started = time.monotonic()
    archive = _search().pareto_search(index, seeds, AXES, started + budget, depth=2)
    elapsed = time.monotonic() - started
    assert elapsed <= budget + 1.0, elapsed
    assert archive
    for point in archive:
        assert isinstance(decode(index, point.choice, point.sequences), Decoded)
