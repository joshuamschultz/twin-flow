"""Five-phase bottleneck procedure for flexible lines (T-144, COMP-014, REQ-102; Pinedo Sec
16.4, p.443-447; SDD "Five-phase flexible line" pseudo-code and Risks).

`twinflow.scheduling.algorithms.five_phase` registers `five_phase` (family `decomposition`,
`deterministic=True`, every `Feature`). Phase 4 runs ATCS on the bottleneck; whether it calls
`atcs` through the registry or reuses the kernel dispatch engine is the implementer's choice,
and nothing here depends on it. API these tests pin (pure functions over a `ProblemIndex`):

- `bottleneck_center(index) -> str`: phase 1, the work center with the largest
  (sum of shortest runs + n * sbar) / (m * available seconds to the horizon) over its
  pending ops (`index.now` is the start of the horizon).
- `bottleneck_windows(index, center_id) -> Mapping[op_id, (r_b, d_b)]`: phase 2, one entry
  per op of the center: r_b = job release + phi * (shortest runs of the job's ops before it),
  d_b = job due - phi * (shortest runs of the job's ops after it), None without a due;
  phi is `index.phi` (default 2, p.446).
- `setup_swaps(index, choice, sequences, bottleneck) -> Mapping[machine_id, Sequence[op_id]]`:
  phase 5's swap pass. Given a decodable plan (`choice` op -> machine, `sequences` machine ->
  op ids), it returns sequences for every machine where machines of the bottleneck center are
  unchanged and each other machine's sequence is reached by adjacent swaps that lower that
  machine's total setup without raising the plan's weighted tardiness.

Example 16.4.1: the book's choice at t=41 (p.446) is not reproducible from its stated ATCS
index (recomputation picks job 6, the book job 5; SDD Risks), so the test gates the method
against FIFO, EDD and SPT and prints its value next to the book's 163.5 (the decode of the
book's sequences, pinned in `test_fixtures.py`).

The leaf is imported per test, so its absence fails each test rather than collection.
"""

from __future__ import annotations

import importlib
import itertools
import math
from collections.abc import Mapping, Sequence
from types import ModuleType
from typing import Any

import pytest
from bench.hmlv import TRAINING_SEEDS, flow_line_instance
from generators import random_request
from kernel_cases import (
    BOOK_16_4_1_WEIGHTED_TARDINESS,
    compile_request,
    load_request,
    machine,
    machine_choice,
    machine_sequences,
    op,
    request_from,
)

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.model import ObjectiveKind, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve

NAME = "five_phase"
WT = ObjectiveKind.WEIGHTED_TARDINESS
WT_OBJECTIVE = {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}
TOL = 1e-6


# --- helpers ---------------------------------------------------------------------------------


def _module() -> ModuleType:
    return importlib.import_module("twinflow.scheduling.algorithms.five_phase")


def _index(request: SolveRequest, phi: float | None = None) -> ProblemIndex:
    problem = compile_request(request).problem
    return ProblemIndex.build(problem) if phi is None else ProblemIndex.build(problem, phi=phi)


def _run(name: str, request: SolveRequest) -> SolveResult:
    result = solve(request, solver=get_solver(name))
    assert result.schedule.status == "FEASIBLE", (name, result.schedule.issues, result.issues)
    assert result.verified, (name, result.issues)
    return result


def _decoded(
    index: ProblemIndex, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> Decoded:
    decoded = decode(index, choice, sequences)
    assert isinstance(decoded, Decoded), decoded
    return decoded


def _wt(index: ProblemIndex, decoded: Decoded) -> float:
    return fast_values(index, decoded, None, 0.0)[WT]


def _machine_setup(index: ProblemIndex, machine_id: str, sequence: Sequence[str]) -> float:
    """Sequence-dependent setup seconds on one machine (machine state before the first op)."""
    previous: list[str | None] = [None, *sequence[:-1]]
    return math.fsum(
        index.setups.seconds(machine_id, prev, nxt)
        for prev, nxt in zip(previous, sequence, strict=True)
    )


def _swapped(sequence: Sequence[str], k: int) -> tuple[str, ...]:
    out = list(sequence)
    out[k], out[k + 1] = out[k + 1], out[k]
    return tuple(out)


def _as_tuples(sequences: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    return {m: tuple(seq) for m, seq in sequences.items()}


def _job(job_id: str, ops: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "weight_override": 1.0, "operations": ops} | fields


def _line_request(
    jobs: list[dict[str, Any]], machines: list[dict[str, Any]], *, setups: bool = False
) -> SolveRequest:
    centers = sorted({m["work_center_id"] for m in machines})
    problem: dict[str, Any] = {
        "id": "five-phase-case",
        "jobs": jobs,
        "machines": machines,
        "work_centers": [{"id": c} for c in centers],
    }
    if setups:
        problem["setup_functions"] = [
            {
                "id": f"S-{m['id']}",
                "terms": [{"kind": "abs_diff", "param": "a", "seconds_per_unit": 1.0}],
            }
            for m in machines
        ]
    return request_from(problem, profile={"objective": WT_OBJECTIVE})


def _two_stage_line(stage_b_run: float) -> SolveRequest:
    """3 jobs, stage A (1 machine, 20 s per op) then stage B (2 machines, `stage_b_run`)."""
    jobs = [
        _job(
            str(j),
            [
                op(f"{j}-A", {"A1": 20.0}, center="A"),
                op(f"{j}-B", {"B1": stage_b_run, "B2": stage_b_run}, center="B", after=[f"{j}-A"]),
            ],
            due=200.0,
        )
        for j in (1, 2, 3)
    ]
    machines = [machine("A1", center="A"), machine("B1", center="B"), machine("B2", center="B")]
    return _line_request(jobs, machines)


def _setup_case(dues: Mapping[str, float | None], center: str = "N") -> SolveRequest:
    """Ops x, y, z (10 s each, setup param a = 0, 5, 0) on one machine of `center`, plus a
    one-op job on another center so the line has two centers. Setups are attached and
    |a - a'| seconds, so x, y, z pays 10 s of setup and a swap can cut it to 5 s."""
    other = "B" if center == "N" else "N"
    machine_id, other_machine = f"{center}1", f"{other}1"
    jobs = [
        _job(
            name,
            [op(f"{name}-1", {machine_id: 10.0}, center=center, setup_params={"a": a})],
            **({"due": dues[name]} if dues.get(name) is not None else {}),
        )
        for name, a in (("x", 0.0), ("y", 5.0), ("z", 0.0))
    ]
    jobs.append(_job("w", [op("w-1", {other_machine: 5.0}, center=other, setup_params={"a": 0.0})]))
    machines = [
        machine(machine_id, center=center, setup_function_id=f"S-{machine_id}"),
        machine(other_machine, center=other, setup_function_id=f"S-{other_machine}"),
    ]
    return _line_request(jobs, machines, setups=True)


XYZ = ("x-1", "y-1", "z-1")


def _setup_case_plan(request: SolveRequest) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    choice = {o.id: o.options[0].machine_id for job in request.problem.jobs for o in job.operations}
    machine_of_x = choice["x-1"]
    other = next(m for m in set(choice.values()) if m != machine_of_x)
    return choice, {machine_of_x: XYZ, other: ("w-1",)}


# --- registration ----------------------------------------------------------------------------


def test_five_phase_is_registered_as_a_deterministic_decomposition_with_every_feature() -> None:
    _module()
    entry = next((info for info in available() if info.name == NAME), None)

    assert entry is not None, [info.name for info in available()]
    assert entry.family == "decomposition"
    assert entry.deterministic is True
    assert entry.capabilities == frozenset(Feature)
    assert entry.module == "twinflow.scheduling.algorithms.five_phase"
    assert get_solver(NAME).capabilities == frozenset(Feature)


# --- phase 1: bottleneck ---------------------------------------------------------------------


def test_bottleneck_on_16_4_1_is_stage_2_the_center_of_m2_and_m3() -> None:
    request = load_request("pinedo_16_4_1")
    index = _index(request)

    center = _module().bottleneck_center(index)

    assert center == "WC-S2"
    assert set(index.machines_of(center)) == {"M2", "M3"}


def test_bottleneck_divides_load_by_machine_count() -> None:
    # A: 60 s on 1 machine (60 per machine); B: 3 x 30 s on 2 machines (45 per machine).
    assert _module().bottleneck_center(_index(_two_stage_line(30.0))) == "A"


def test_bottleneck_moves_to_the_parallel_stage_when_its_per_machine_load_is_larger() -> None:
    # A: 60 per machine; B: 3 x 45 s on 2 machines = 67.5 per machine.
    assert _module().bottleneck_center(_index(_two_stage_line(45.0))) == "B"


# --- phase 2: windows ------------------------------------------------------------------------

# Stage 1 and stage 3 runs are p_j / 4 (speed 4); stage 2 runs p_j (speed 1).
P_16_4_1 = {"1": 16.0, "2": 24.0, "3": 20.0, "4": 32.0, "5": 28.0, "6": 22.0}
R_16_4_1 = {"1": 1.0, "2": 5.0, "3": 9.0, "4": 7.0, "5": 15.0, "6": 6.0}
D_16_4_1 = {"1": 30.0, "2": 35.0, "3": 60.0, "4": 71.0, "5": 27.0, "6": 63.0}


@pytest.mark.parametrize("phi", [2.0, 1.0])
def test_windows_on_16_4_1_stage_2_add_phi_times_upstream_and_subtract_downstream(
    phi: float,
) -> None:
    index = _index(load_request("pinedo_16_4_1"), phi=None if phi == 2.0 else phi)

    windows = _module().bottleneck_windows(index, "WC-S2")

    expected = {
        f"{j}-2": (R_16_4_1[j] + phi * P_16_4_1[j] / 4, D_16_4_1[j] - phi * P_16_4_1[j] / 4)
        for j in P_16_4_1
    }
    assert set(windows) == set(expected)
    for op_id, (r_b, d_b) in expected.items():
        got_r, got_d = windows[op_id]
        assert got_r == pytest.approx(r_b, abs=1e-9), op_id
        assert got_d is not None
        assert got_d == pytest.approx(d_b, abs=1e-9), op_id


def test_windows_on_16_4_1_default_phi_matches_the_book_values() -> None:
    windows = _module().bottleneck_windows(_index(load_request("pinedo_16_4_1")), "WC-S2")

    assert {o: (w[0], w[1]) for o, w in windows.items()} == {
        "1-2": (9.0, 22.0),
        "2-2": (17.0, 23.0),
        "3-2": (19.0, 50.0),
        "4-2": (23.0, 55.0),
        "5-2": (29.0, 13.0),
        "6-2": (17.0, 52.0),
    }


def test_windows_first_and_last_stage_and_a_job_without_due() -> None:
    jobs = [
        _job(
            "1",
            [
                op("1-A", {"A1": 20.0}, center="A"),
                op("1-B", {"B1": 30.0, "B2": 40.0}, center="B", after=["1-A"]),
            ],
            due=100.0,
            release=3.0,
        ),
        _job(
            "2",
            [
                op("2-A", {"A1": 10.0}, center="A"),
                op("2-B", {"B1": 50.0, "B2": 25.0}, center="B", after=["2-A"]),
            ],
        ),
    ]
    machines = [machine("A1", center="A"), machine("B1", center="B"), machine("B2", center="B")]
    index = _index(_line_request(jobs, machines))
    module = _module()

    first = module.bottleneck_windows(index, "A")
    last = module.bottleneck_windows(index, "B")

    # Center A: no upstream; downstream is the shortest option of the B op.
    assert first["1-A"] == pytest.approx((3.0, 100.0 - 2.0 * 30.0))
    assert first["2-A"][0] == pytest.approx(0.0)
    assert first["2-A"][1] is None
    # Center B: upstream 20 / 10 s; no downstream, so d_b is the job due.
    assert last["1-B"] == pytest.approx((3.0 + 2.0 * 20.0, 100.0))
    assert last["2-B"][0] == pytest.approx(2.0 * 10.0)
    assert last["2-B"][1] is None


# --- phase 5: setup swaps (crafted) ----------------------------------------------------------


def test_swap_pass_takes_a_setup_cutting_swap_when_no_job_can_be_late() -> None:
    request = _setup_case({})
    index = _index(request)
    choice, before = _setup_case_plan(request)
    wt_before = _wt(index, _decoded(index, choice, before))

    after = _as_tuples(_module().setup_swaps(index, choice, before, "B"))

    assert sorted(after["N1"]) == sorted(XYZ)
    assert after["B1"] == ("w-1",)
    assert _machine_setup(index, "N1", XYZ) == pytest.approx(10.0)
    assert _machine_setup(index, "N1", after["N1"]) < 10.0 - TOL
    assert _wt(index, _decoded(index, choice, after)) <= wt_before + TOL


def test_swap_pass_refuses_every_swap_that_would_raise_weighted_tardiness() -> None:
    # x due 10 and y due 25 are exactly met by x, y, z; each setup-cutting swap makes one late.
    request = _setup_case({"x": 10.0, "y": 25.0})
    index = _index(request)
    choice, before = _setup_case_plan(request)
    wt_before = _wt(index, _decoded(index, choice, before))
    assert wt_before == pytest.approx(0.0)
    for k in range(len(XYZ) - 1):  # premise: no adjacent swap is both cheaper and harmless
        swapped = before | {"N1": _swapped(XYZ, k)}
        cheaper = _machine_setup(index, "N1", swapped["N1"]) < 10.0 - TOL
        harmless = _wt(index, _decoded(index, choice, swapped)) <= wt_before + TOL
        assert not (cheaper and harmless), k

    after = _as_tuples(_module().setup_swaps(index, choice, before, "B"))

    assert after["N1"] == XYZ
    assert _wt(index, _decoded(index, choice, after)) <= wt_before + TOL


def test_swap_pass_never_touches_a_bottleneck_machine() -> None:
    request = _setup_case({}, center="B")  # the setup-cutting case, now on the bottleneck
    index = _index(request)
    choice, before = _setup_case_plan(request)

    after = _as_tuples(_module().setup_swaps(index, choice, before, "B"))

    assert after == before


def test_swap_pass_never_raises_weighted_tardiness_on_any_order_of_the_crafted_case() -> None:
    request = _setup_case({"x": 25.0, "y": 15.0, "z": 30.0})
    index = _index(request)
    module = _module()
    for order in itertools.permutations(XYZ):
        choice, plan = _setup_case_plan(request)
        before = plan | {"N1": order}
        decoded = _decoded(index, choice, before)

        after = _as_tuples(module.setup_swaps(index, choice, before, "B"))

        assert sorted(after["N1"]) == sorted(XYZ), order
        assert _wt(index, _decoded(index, choice, after)) <= _wt(index, decoded) + TOL, order
        assert (
            _machine_setup(index, "N1", after["N1"]) <= _machine_setup(index, "N1", order) + TOL
        ), order


# --- phase 5: setup swaps (property over flow-line HMLV) -------------------------------------


@pytest.mark.parametrize("seed", TRAINING_SEEDS)
def test_swap_pass_on_flow_line_hmlv_never_raises_weighted_tardiness(seed: int) -> None:
    request = flow_line_instance(seed)
    index = _index(request)
    module = _module()
    plan = _run("edd", request).schedule
    choice = machine_choice(plan)
    before = _as_tuples(machine_sequences(plan))
    bottleneck = module.bottleneck_center(index)
    wt_before = _wt(index, _decoded(index, choice, before))

    after = _as_tuples(module.setup_swaps(index, choice, before, bottleneck))

    assert _wt(index, _decoded(index, choice, after)) <= wt_before + TOL * max(1.0, wt_before)
    for machine_id, sequence in before.items():
        assert sorted(after[machine_id]) == sorted(sequence), machine_id
        if machine_id in index.machines_of(bottleneck):
            assert after[machine_id] == sequence, machine_id
        else:
            assert (
                _machine_setup(index, machine_id, after[machine_id])
                <= _machine_setup(index, machine_id, sequence) + TOL
            ), machine_id


# --- the solver on 16.4.1 --------------------------------------------------------------------


def test_16_4_1_verified_and_no_worse_than_fifo_edd_and_spt() -> None:
    request = load_request("pinedo_16_4_1")
    _module()

    five = _run(NAME, request).objective_values[WT]
    rules = {name: _run(name, request).objective_values[WT] for name in ("fifo", "edd", "spt")}

    print(
        f"\n16.4.1 weighted tardiness: five_phase {five} (book {BOOK_16_4_1_WEIGHTED_TARDINESS}); "
        + ", ".join(f"{name} {value}" for name, value in rules.items())
    )
    for name, value in rules.items():
        assert five <= value + TOL, (name, five, value)


def test_16_4_1_assigns_every_op_and_uses_both_stage_2_machines() -> None:
    request = load_request("pinedo_16_4_1")
    _module()

    result = _run(NAME, request)

    ops = [a.operation_id for a in result.schedule.assignments]
    assert sorted(ops) == sorted(f"{j}-{s}" for j in "123456" for s in "123")
    stage_2 = {a.machine_id for a in result.schedule.assignments if a.operation_id.endswith("-2")}
    assert stage_2 == {"M2", "M3"}


def test_five_phase_is_deterministic() -> None:
    request = load_request("pinedo_16_4_1")
    _module()

    first = _run(NAME, request).schedule.model_dump_json(include={"status", "assignments"})
    second = _run(NAME, request).schedule.model_dump_json(include={"status", "assignments"})

    assert first == second


def test_a_single_center_line_has_no_upstream_or_downstream_and_still_verifies() -> None:
    jobs = [
        _job(str(j), [op(f"{j}-1", {"B1": 10.0 + j, "B2": 12.0}, center="B")], due=15.0)
        for j in range(5)
    ]
    request = _line_request(jobs, [machine("B1", center="B"), machine("B2", center="B")])
    module = _module()

    assert module.bottleneck_center(_index(request)) == "B"
    result = _run(NAME, request)
    assert len(result.schedule.assignments) == 5


# --- every feature (adversarial) -------------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_five_phase_on_generated_problems_keeps_the_contract(seed: int) -> None:
    _module()
    result = solve(random_request(seed), solver=get_solver(NAME))

    codes = {issue.code for issue in result.issues}
    assert "solver_error" not in codes, (seed, result.issues)
    assert "unsupported_feature" not in codes, (seed, result.issues)
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified, (seed, result.issues)
        ops = [a.operation_id for a in result.schedule.assignments]
        assert len(ops) == len(set(ops)), seed
    else:
        assert result.schedule.issues or result.issues, seed
