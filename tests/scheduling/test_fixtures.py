"""Oracle tests for the T-126 Pinedo fixtures (COMP-027, REQ-125).

Each fixture encodes a worked example of Pinedo, Scheduling: Theory, Algorithms, and
Systems (5th ed.) and cites its printed page in every job's `customer` text. The optima
come from the independent oracle (`brute_force`), or from an exact subset DP written here
when the instance is too large to enumerate quickly (15.2.3, 9! sequences).

- 3.3.3 (p.49-50): minimum number of tardy jobs is 2 (Moore-Hodgson, book order 3,4,5,1,2).
- 7.2.3 (p.195-196): makespan optimum 8.
- 7.3.1 (p.200-203): weighted tardiness optimum 18.
- 14.3.4 (p.383-384, also 15.1.1 p.397-398): weighted tardiness optimum 408 at 1,4,2,3.
- 15.2.3 (p.402): book optimum 0 with job 1 weight 0. The model needs weights > 0, so job 1
  carries EPSILON_WEIGHT; the optimum is then 35 * EPSILON_WEIGHT (job 1 last at C = 57,
  every other job on time), which is 0 under the book's weights.
- 16.4.1 (p.445-447): the book's sequences decode to C = 25, 43, 93, 62, 75, 81.5 and
  weighted tardiness 163.5 with detached setups, 167.5 with attached setups.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from pathlib import Path

import pytest
from oracle import brute_force, combination_count, semi_active
from oracle_math import objective_values

from twinflow.scheduling.model import (
    CompiledProblem,
    ObjectiveKind,
    Problem,
    Schedule,
    SolveRequest,
)
from twinflow.scheduling.validator import validate

FIXTURES = Path(__file__).parent / "fixtures"
K = ObjectiveKind
EPSILON_WEIGHT = 1e-06
"""Stand-in for the book's weight 0 on job 1 of Example 15.2.3 (PosFloat forbids 0)."""

CITATIONS = {
    "pinedo_3_3_3.json": ("3.3.3", "Example 3.3.3, p.49-50"),
    "pinedo_7_2_3.json": ("7.2.3", "Example 7.2.3, p.195-196"),
    "pinedo_7_3_1.json": ("7.3.1", "Example 7.3.1, p.200-203"),
    "pinedo_14_3_4.json": ("14.3.4", "Example 14.3.4, p.383-384"),
    "pinedo_15_2_3.json": ("15.2.3", "Example 15.2.3, p.402"),
    "pinedo_16_4_1.json": ("16.4.1", "Example 16.4.1, p.445-447"),
}

BOOK_SEQUENCES_16_4_1: dict[str, tuple[str, ...]] = {
    # Pinedo p.446-447, Fig. 16.7: stage 1 runs in the stage-2 start order 1,2,4,5,6,3;
    # ATCS puts 1,4,6 on machine 2 and 2,5,3 on machine 3; stage 3 runs in the stage-2
    # completion order 1,2,4,5,6,3. Op "j-s" is job j at stage s.
    "M1": ("1-1", "2-1", "4-1", "5-1", "6-1", "3-1"),
    "M2": ("1-2", "4-2", "6-2"),
    "M3": ("2-2", "5-2", "3-2"),
    "M4": ("1-3", "2-3", "4-3", "5-3", "6-3", "3-3"),
}


def _load(name: str) -> SolveRequest:
    return SolveRequest.model_validate_json((FIXTURES / name).read_text())


def _values(request: SolveRequest, schedule: Schedule) -> dict[ObjectiveKind, float]:
    compiled = CompiledProblem(problem=request.problem, profile=request.profile)
    return objective_values(compiled, schedule)


def _single_machine_order(schedule: Schedule) -> list[str]:
    ordered = sorted(schedule.assignments, key=lambda a: a.start)
    return [a.operation_id.split("-")[0] for a in ordered]


def _job_completions(schedule: Schedule) -> dict[str, float]:
    finish: dict[str, float] = {}
    for a in schedule.assignments:
        job_id = a.operation_id.split("-")[0]
        finish[job_id] = max(finish.get(job_id, a.end), a.end)
    return finish


def _min_weighted_tardiness(
    p: Sequence[float], d: Sequence[float], w: Sequence[float]
) -> tuple[float, tuple[int, ...]]:
    """Exact 1 || sum w_j T_j by DP over job subsets: the subset runs first, its last job ends
    at the subset's total processing time. Returns the optimum and one optimal order (0-based).
    """
    n = len(p)
    best: dict[int, tuple[float, tuple[int, ...]]] = {0: (0.0, ())}
    for mask in range(1, 1 << n):
        members = [j for j in range(n) if mask >> j & 1]
        end = math.fsum(p[j] for j in members)
        best[mask] = min(
            (
                best[mask ^ (1 << j)][0] + w[j] * max(end - d[j], 0.0),
                (*best[mask ^ (1 << j)][1], j),
            )
            for j in members
        )
    return best[(1 << n) - 1]


def _single_machine_data(problem: Problem) -> tuple[list[float], list[float], list[float]]:
    p = [job.operations[0].options[0].fixed_seconds for job in problem.jobs]
    d = [job.due if job.due is not None else math.inf for job in problem.jobs]
    w = [job.weight_override or 1.0 for job in problem.jobs]
    return p, d, w


def _with_attached_setups(problem: Problem) -> Problem:
    jobs = tuple(
        job.model_copy(
            update={
                "operations": tuple(
                    op.model_copy(update={"setup_detached": False}) for op in job.operations
                )
            }
        )
        for job in problem.jobs
    )
    return problem.model_copy(update={"jobs": jobs})


# --- every fixture -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(CITATIONS))
def test_fixture_validates_with_no_issues(name: str) -> None:
    request = _load(name)

    assert validate(request) == []


@pytest.mark.parametrize("name", sorted(CITATIONS))
def test_fixture_cites_the_book_example_and_page_on_every_job(name: str) -> None:
    example, citation = CITATIONS[name]
    problem = _load(name).problem

    assert problem.id == f"pinedo-{example}"
    for job in problem.jobs:
        assert job.customer is not None
        assert "Pinedo, Scheduling" in job.customer
        assert citation in job.customer


# --- 3.3.3 -------------------------------------------------------------------------------------


def test_3_3_3_encodes_the_book_processing_times_and_due_dates() -> None:
    p, d, _ = _single_machine_data(_load("pinedo_3_3_3.json").problem)

    assert (p, d) == ([7, 8, 4, 6, 6], [9, 17, 18, 19, 21])


def test_3_3_3_minimum_number_of_tardy_jobs_is_2() -> None:
    request = _load("pinedo_3_3_3.json")

    value, _ = brute_force(request.problem, K.TARDY_COUNT, request.profile)

    assert value == 2.0


def test_3_3_3_moore_hodgson_order_3_4_5_1_2_has_jobs_3_4_5_on_time() -> None:
    request = _load("pinedo_3_3_3.json")

    schedule = semi_active(request.problem, {"M1": ["3-1", "4-1", "5-1", "1-1", "2-1"]})

    due = {job.id: job.due for job in request.problem.jobs}
    on_time = {j for j, c in _job_completions(schedule).items() if c <= (due[j] or math.inf)}
    assert on_time == {"3", "4", "5"}
    assert _values(request, schedule)[K.TARDY_COUNT] == 2.0


# --- 7.2.3 -------------------------------------------------------------------------------------


def test_7_2_3_encodes_the_book_routes() -> None:
    problem = _load("pinedo_7_2_3.json").problem

    routes = {
        job.id: [(op.options[0].machine_id, op.options[0].fixed_seconds) for op in job.operations]
        for job in problem.jobs
    }

    assert routes == {
        "1": [("M1", 1), ("M2", 1)],
        "2": [("M2", 1), ("M1", 1)],
        "3": [("M3", 4)],
        "4": [("M3", 4)],
    }


def test_7_2_3_makespan_optimum_is_8() -> None:
    request = _load("pinedo_7_2_3.json")

    value, schedule = brute_force(request.problem, K.MAKESPAN, request.profile)

    assert value == 8.0
    assert max(a.end for a in schedule.assignments) == 8.0


# --- 7.3.1 -------------------------------------------------------------------------------------


def test_7_3_1_encodes_weights_releases_dues_and_routes() -> None:
    problem = _load("pinedo_7_3_1.json").problem

    table = {
        job.id: (
            job.weight_override,
            job.release,
            job.due,
            [(op.options[0].machine_id, op.options[0].fixed_seconds) for op in job.operations],
        )
        for job in problem.jobs
    }

    assert table == {
        "1": (1.0, 5.0, 24.0, [("M1", 5), ("M2", 10), ("M3", 4)]),
        "2": (2.0, 0.0, 18.0, [("M3", 4), ("M1", 5), ("M2", 6)]),
        "3": (2.0, 0.0, 16.0, [("M3", 5), ("M2", 3), ("M1", 7)]),
    }


def test_7_3_1_weighted_tardiness_optimum_is_18() -> None:
    request = _load("pinedo_7_3_1.json")

    value, _ = brute_force(request.problem, K.WEIGHTED_TARDINESS, request.profile)

    assert value == 18.0


# --- 14.3.4 / 15.1.1 ---------------------------------------------------------------------------


def test_14_3_4_encodes_the_book_data() -> None:
    p, d, w = _single_machine_data(_load("pinedo_14_3_4.json").problem)

    assert (p, d, w) == ([10, 10, 13, 4], [4, 2, 1, 12], [14, 12, 1, 12])


def test_14_3_4_weighted_tardiness_optimum_is_408_at_1_4_2_3() -> None:
    request = _load("pinedo_14_3_4.json")

    value, schedule = brute_force(request.problem, K.WEIGHTED_TARDINESS, request.profile)

    assert value == 408.0
    assert _single_machine_order(schedule) == ["1", "4", "2", "3"]


def test_14_3_4_optimum_1_4_2_3_is_unique() -> None:
    request = _load("pinedo_14_3_4.json")
    ops = [job.operations[0].id for job in request.problem.jobs]

    optimal = [
        order
        for order in itertools.permutations(ops)
        if _values(request, semi_active(request.problem, {"M1": order}))[K.WEIGHTED_TARDINESS]
        == 408.0
    ]

    assert optimal == [("1-1", "4-1", "2-1", "3-1")]


def test_subset_dp_agrees_with_brute_force_on_14_3_4() -> None:
    problem = _load("pinedo_14_3_4.json").problem

    value, order = _min_weighted_tardiness(*_single_machine_data(problem))

    assert value == 408.0
    assert order == (0, 3, 1, 2)


# --- 15.2.3 ------------------------------------------------------------------------------------


def test_15_2_3_encodes_book_data_with_epsilon_for_the_zero_weight() -> None:
    problem = _load("pinedo_15_2_3.json").problem

    p, d, w = _single_machine_data(problem)

    assert p == [7, 6, 7, 6, 5, 5, 8, 9, 4]
    assert d == [22, 24, 24, 25, 33, 51, 52, 52, 55]
    assert w == [EPSILON_WEIGHT, 1, 1, 2, 1, 1, 2, 1, 4]


def test_15_2_3_too_large_for_brute_force_so_the_subset_dp_is_used() -> None:
    problem = _load("pinedo_15_2_3.json").problem

    assert combination_count(problem) == math.factorial(9)


def test_15_2_3_optimum_is_35_epsilon_with_job_1_last() -> None:
    problem = _load("pinedo_15_2_3.json").problem

    value, order = _min_weighted_tardiness(*_single_machine_data(problem))

    assert value == pytest.approx(35 * EPSILON_WEIGHT, rel=1e-12)
    assert order[-1] == 0


def test_15_2_3_optimum_under_the_book_weights_is_0() -> None:
    request = _load("pinedo_15_2_3.json")
    p, d, w = _single_machine_data(request.problem)
    _, order = _min_weighted_tardiness(p, d, w)
    sequence = [request.problem.jobs[j].operations[0].id for j in order]

    schedule = semi_active(request.problem, {"M1": sequence})

    finish = _job_completions(schedule)
    book_weights = dict(zip("123456789", [0, 1, 1, 2, 1, 1, 2, 1, 4], strict=True))
    tardiness = {
        job.id: max(finish[job.id] - (job.due or 0.0), 0.0) for job in request.problem.jobs
    }
    assert finish["1"] == 57.0
    assert math.fsum(book_weights[j] * t for j, t in tardiness.items()) == 0.0
    assert _values(request, schedule)[K.WEIGHTED_TARDINESS] == pytest.approx(
        35 * EPSILON_WEIGHT, rel=1e-12
    )


# --- 16.4.1 ------------------------------------------------------------------------------------


def test_16_4_1_encodes_speeds_settings_and_detached_setups() -> None:
    problem = _load("pinedo_16_4_1.json").problem

    seconds = {
        (op.id, o.machine_id): o.fixed_seconds
        for job in problem.jobs
        for op in job.operations
        for o in op.options
    }
    settings = {op.id: op.setup_params["a"] for job in problem.jobs for op in job.operations}

    assert [seconds[(f"{j}-1", "M1")] for j in "123456"] == [4, 6, 5, 8, 7, 5.5]
    assert [seconds[(f"{j}-2", "M2")] for j in "123456"] == [16, 24, 20, 32, 28, 22]
    assert [seconds[(f"{j}-2", "M3")] for j in "123456"] == [16, 24, 20, 32, 28, 22]
    assert [seconds[(f"{j}-3", "M4")] for j in "123456"] == [4, 6, 5, 8, 7, 5.5]
    assert [settings[f"{j}-1"] for j in "123456"] == [4, 2, 3, 1, 4, 2]
    assert [settings[f"{j}-2"] for j in "123456"] == [3, 4, 1, 3, 1, 3]
    assert [settings[f"{j}-3"] for j in "123456"] == [2, 2, 4, 1, 2, 3]
    assert all(op.setup_detached for job in problem.jobs for op in job.operations)
    assert all(not m.state.last_setup_params for m in problem.machines)


def test_16_4_1_book_sequences_decode_to_the_book_completions_and_163_5() -> None:
    request = _load("pinedo_16_4_1.json")

    schedule = semi_active(request.problem, BOOK_SEQUENCES_16_4_1)

    finish = _job_completions(schedule)
    assert [finish[j] for j in "123456"] == [25.0, 43.0, 93.0, 62.0, 75.0, 81.5]
    assert _values(request, schedule)[K.WEIGHTED_TARDINESS] == 163.5


def test_16_4_1_book_weighted_tardiness_terms_are_0_16_33_0_96_18_5() -> None:
    request = _load("pinedo_16_4_1.json")

    finish = _job_completions(semi_active(request.problem, BOOK_SEQUENCES_16_4_1))

    terms = [
        (job.weight_override or 0.0) * max(finish[job.id] - (job.due or 0.0), 0.0)
        for job in request.problem.jobs
    ]
    assert terms == [0.0, 16.0, 33.0, 0.0, 96.0, 18.5]


def test_16_4_1_book_sequences_with_attached_setups_give_167_5() -> None:
    request = _load("pinedo_16_4_1.json")
    attached = _with_attached_setups(request.problem)

    schedule = semi_active(attached, BOOK_SEQUENCES_16_4_1)

    compiled = CompiledProblem(problem=attached, profile=request.profile)
    assert objective_values(compiled, schedule)[K.WEIGHTED_TARDINESS] == 167.5


def test_16_4_1_first_op_on_each_machine_has_zero_setup() -> None:
    request = _load("pinedo_16_4_1.json")

    schedule = semi_active(request.problem, BOOK_SEQUENCES_16_4_1)

    placed = {a.operation_id: a for a in schedule.assignments}
    firsts = [seq[0] for seq in BOOK_SEQUENCES_16_4_1.values()]
    assert [placed[op_id].setup_end - placed[op_id].start for op_id in firsts] == [0.0] * 4
