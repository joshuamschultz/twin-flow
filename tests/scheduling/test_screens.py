"""Moore-Hodgson on-time screen (T-153, COMP-011, REQ-096; Pinedo Alg 3.3.1, p.47-48).

`twinflow.scheduling.algorithms.screens` is an analysis, not a solver. API these tests pin:

- `on_time_screen(compiled, work_center_id=None) -> OnTimeScreen`: Moore-Hodgson on one
  work center. Each job with an op at the center is one single-machine job with
  p = the op's shortest eligible run / m (m = machines of the center) and due = the op's
  local due `dloc` (`ProblemIndex.local_due`, phi = 2: due - 2 x the job's later shortest
  runs). Jobs go in EDD order of dloc; a job whose completion exceeds its dloc removes the
  longest job of the on-time set so far. `None` picks the bottleneck: the center with the
  highest load ratio (sum of its pending ops' shortest runs over its machine capacity).
- `OnTimeScreen` (dataclass) `{work_center_id, on_time: tuple[job_id, ...], late: tuple[
  job_id, ...]}`, both tuples in EDD (dloc) order.
- Registration (SDD COMP-011 "registered as an analysis entry, not a solver"; the same
  shape as `RULES`, DEV-23): a leaf may export `ANALYSES: tuple[registry.AnalysisSpec, ...]`
  with `AnalysisSpec(name, function)`, and `registry.analysis(name)` returns the function
  (ValueError for an unknown name). `screens` exports `SOLVERS = ()`, so `available()` lists
  no solver from it.

The leaf is imported per test, so its absence fails each test rather than collection.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import ModuleType

import pytest
from kernel_cases import compile_request, load_request, machine, op, request_from

from twinflow.scheduling import registry
from twinflow.scheduling.model import SolveRequest

LEAF = "twinflow.scheduling.algorithms.screens"
ALGORITHMS = "twinflow.scheduling.algorithms"


def _leaf() -> ModuleType:
    return importlib.import_module(LEAF)


def _two_center_request(b_runs: tuple[float, ...] = (2.0, 1.0, 3.0, 1.0)) -> SolveRequest:
    """Four jobs, each an op at center A (machines A1, A2) then an op at center B (B1).

    A runs (A1, A2): J1 (10, 12), J2 (9, 8), J3 (6, 7), J4 (12, 20); dues 10, 9, 15, 17.
    With the default B runs (2, 1, 3, 1): p on A = shortest / 2 = 5, 4, 3, 6 and
    dloc on A = due - 2 x B run = 6, 7, 9, 15.
    """
    a_runs = (
        {"A1": 10.0, "A2": 12.0},
        {"A1": 9.0, "A2": 8.0},
        {"A1": 6.0, "A2": 7.0},
        {"A1": 12.0, "A2": 20.0},
    )
    dues = (10.0, 9.0, 15.0, 17.0)
    jobs = [
        {
            "id": f"J{k}",
            "due": dues[k - 1],
            "operations": [
                op(f"J{k}-1", a_runs[k - 1], center="A"),
                op(f"J{k}-2", {"B1": b_runs[k - 1]}, center="B", after=[f"J{k}-1"]),
            ],
        }
        for k in range(1, 5)
    ]
    problem = {
        "id": "screen-two-centers",
        "jobs": jobs,
        "machines": [
            machine("A1", center="A"),
            machine("A2", center="A"),
            machine("B1", center="B"),
        ],
        "work_centers": [{"id": "B"}, {"id": "A"}],
    }
    return request_from(problem)


# --- Pinedo Example 3.3.3 ---------------------------------------------------------------------


@pytest.mark.parametrize("center", [None, "WC-M1"])
def test_on_time_screen_example_3_3_3_on_time_3_4_5_late_1_2(center: str | None) -> None:
    # p = 7, 8, 4, 6, 6 and d = 9, 17, 18, 19, 21: job 3 removes job 2 (p 8), job 5 removes
    # job 1 (p 7), leaving 3, 4, 5 on time (Pinedo p.49-50).
    compiled = compile_request(load_request("pinedo_3_3_3"))
    screen = _leaf().on_time_screen(compiled, center)
    assert screen.work_center_id == "WC-M1"
    assert tuple(screen.on_time) == ("3", "4", "5")
    assert tuple(screen.late) == ("1", "2")


# --- multi-machine center ---------------------------------------------------------------------


def test_on_time_screen_multi_machine_center_uses_shortest_run_over_m_and_local_due() -> None:
    # EDD by dloc: J1 (6), J2 (7), J3 (9), J4 (15). C = 5 <= 6; J2: 9 > 7 removes J1 (p 5);
    # J3: 7 <= 9; J4: 13 <= 15. Raw dues give late {J4}; mean run / m gives late {J1, J4};
    # the full shortest run with dloc gives on-time {J3} only.
    compiled = compile_request(_two_center_request())
    screen = _leaf().on_time_screen(compiled, "A")
    assert screen.work_center_id == "A"
    assert tuple(screen.on_time) == ("J2", "J3", "J4")
    assert tuple(screen.late) == ("J1",)


def test_on_time_screen_last_center_uses_the_job_due_as_local_due() -> None:
    # B is the last op of every job, so dloc = due: 10, 9, 15, 17 and p = 8, 7, 8, 7 (m 1).
    # EDD J2, J1, J3, J4: 7; 15 > 10 removes J1 (8); 15 <= 15; 22 > 17 removes J3 (8).
    compiled = compile_request(_two_center_request((8.0, 7.0, 8.0, 7.0)))
    screen = _leaf().on_time_screen(compiled, "B")
    assert tuple(screen.on_time) == ("J2", "J4")
    assert tuple(screen.late) == ("J1", "J3")


# --- default center ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("b_runs", "expected"),
    [
        # A: 36 s over 2 machines = 18 per machine; B: 7 s over 1 machine.
        ((2.0, 1.0, 3.0, 1.0), "A"),
        # A still has more total work (36 > 30), but B's 30 s sit on one machine: B is the
        # bottleneck by load ratio, not by total load, op count or machine count.
        ((8.0, 7.0, 8.0, 7.0), "B"),
    ],
)
def test_on_time_screen_default_center_is_the_highest_load_ratio_center(
    b_runs: tuple[float, ...], expected: str
) -> None:
    compiled = compile_request(_two_center_request(b_runs))
    assert _leaf().on_time_screen(compiled, None).work_center_id == expected


def test_on_time_screen_every_job_at_the_center_is_on_time_or_late_exactly_once() -> None:
    compiled = compile_request(_two_center_request())
    screen = _leaf().on_time_screen(compiled, "A")
    jobs = [*screen.on_time, *screen.late]
    assert sorted(jobs) == ["J1", "J2", "J3", "J4"]


# --- registration and isolation ---------------------------------------------------------------


def test_screen_is_registered_as_an_analysis_not_a_solver() -> None:
    leaf = _leaf()
    assert registry.analysis("on_time_screen") is leaf.on_time_screen
    assert all(info.module != LEAF for info in registry.available())
    assert "on_time_screen" not in {info.name for info in registry.available()}
    assert not registry.is_registered("on_time_screen")


def test_unknown_analysis_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="no_such_analysis"):
        registry.analysis("no_such_analysis")


def test_screens_imports_no_other_algorithm_module() -> None:
    path = Path(_leaf().__file__ or "")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offending: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                offending.append(f"relative import {node.module}")
                continue
            names = [node.module or ""]
            if node.module == "twinflow.scheduling":
                names += [f"{ALGORITHMS}.{a.name}" for a in node.names if a.name == "algorithms"]
            if node.module == ALGORITHMS:
                names += [f"{ALGORITHMS}.{alias.name}" for alias in node.names]
        else:
            continue
        offending += [n for n in names if n.startswith(f"{ALGORITHMS}.") and n != LEAF]
    assert offending == []
