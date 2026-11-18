"""Construction solvers: Giffler-Thompson, forward and backward loading (T-149, COMP-012).

REQ-097, REQ-098; SDD "Active mode" (Alg 7.1.3, p.183-184) and COMP-012 (p.473-474).

API these tests pin (the implementer builds to it):

- `twinflow.scheduling.algorithms.construction` registers `gt_active`, `forward_load` and
  `backward_load` (family `construction`, `deterministic=True`).
- Param `rule` (through `profile.method_params[name]`), default `atcs`: any registered
  `dispatch`-family rule name, or `atc` / `atcs`. Any other value is refused as UNKNOWN
  with an `invalid_method_param` issue (the ATCS convention).
- `gt_active`: the dispatch engine in active mode with that rule; every schedule it
  returns is active (see `_movable_ops`).
- `forward_load`: ops in rule priority order from `now`, each at its earliest feasible
  start on its machine, gaps included (an op may land before ops of higher priority).
- `backward_load`: sequences built backward from due dates on the time-mirrored
  instance, then decoded forward.

Every run goes through `solve(request, solver=get(name))`, so each plan is also verified.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

import pytest
from generators import random_request
from kernel_cases import compile_request, load_request, machine, op, request_from, without_features

from twinflow.scheduling.kernel.dispatch import DispatchEngine, gt_branch
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import Assignment, Operation, Problem, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.setups import SetupTable
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate

NAMES = ("gt_active", "forward_load", "backward_load")
MODULE = "twinflow.scheduling.algorithms.construction"
EPS = 1e-6
ACTIVE_SEEDS = range(50)
MIN_ACTIVE_CHECKED = 40
ACTIVE_STRIP = ("calendars", "pause", "operators", "batching", "restart")


# --- helpers ---------------------------------------------------------------------------------


def _with_params(request: SolveRequest, name: str, params: Mapping[str, Any]) -> SolveRequest:
    data = request.model_dump(mode="json")
    data["profile"]["method_params"] = {name: dict(params)}
    return SolveRequest.model_validate(data)


def _solve(name: str, request: SolveRequest, rule: str | None = None) -> SolveResult:
    if rule is not None:
        request = _with_params(request, name, {"rule": rule})
    return solve(request, solver=get_solver(name))


def _run(name: str, request: SolveRequest, rule: str | None = None) -> SolveResult:
    result = _solve(name, request, rule)
    assert result.schedule.status == "FEASIBLE", (name, rule, result.schedule.issues)
    assert result.verified, (name, rule, result.issues)
    return result


def _by_op(result: SolveResult) -> dict[str, Assignment]:
    return {a.operation_id: a for a in result.schedule.assignments}


def _order(result: SolveResult, machine_id: str) -> list[str]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return [a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.end, a.operation_id))]


def _dispatch_rule_names() -> list[str]:
    rules = sorted(i.name for i in available() if i.family == "dispatch")
    assert rules, "no dispatch rule registered"
    return [*rules, "atc", "atcs"]


# --- the active check ------------------------------------------------------------------------
#
# Verifier semantics, machine choice fixed. Op o on machine i (in-progress ops never move)
# is MOVABLE when, with every other op kept where it is, o can be placed at an earlier start
# either in place (left shift) or in an earlier idle gap of i, between consecutive ops
# `prev` and `nxt` (or between i's availability max(now, available_from) and its first op):
#
#   s       = max(gap start, now/available_from, release, pin.earliest_start,
#                 every predecessor's end (or transfer lot) + min_lag,
#                 transfer-lot end floor - run)                     [o's material gate]
#   end'    = s + setup(prev -> o) + run(o)
#   nxt     keeps its exact assignment: end' <= start(nxt) and setup(o -> nxt) equals
#           its current setup(prev -> nxt)
#   removal o's old successor z on i keeps its exact assignment: setup(w -> z) from o's old
#           predecessor w equals its current setup(o -> z)
#   max lag every successor's gate (start, or setup_end when detached) stays
#           <= o's new end (or transfer) + max_lag
#
# Without setups every setup term is 0 and the check is EXACT (a gap that fits o's run
# after its gate is a move that delays nobody). With setups it is CONSERVATIVE: it treats
# o's setup as attached (setup start >= gate, stricter than the detached rule), counts a
# move only when no other op's assignment changes at all (a neighbour whose setup would
# shrink or grow is "another op delayed or moved": Giffler-Thompson compares completions
# with the setup from the machine's last op and cannot see a later shrink), and ignores
# restart warm-ups (the property problems strip restart rules), so it may miss a move but
# never reports one that would change another op's assignment or break a rule.
# would delay another op or break a rule.


class _Plan:
    """A solved plan read with the verifier's semantics (compiled problem, assignments)."""

    def __init__(self, problem: Problem, assignments: tuple[Assignment, ...]) -> None:
        self.problem = problem
        self.setups = SetupTable.for_problem(problem)
        self.by_op = {a.operation_id: a for a in assignments}
        self.ops: dict[str, Operation] = {}
        self.release: dict[str, float] = {}
        self.successors: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for job in problem.jobs:
            for operation in job.operations:
                self.ops[operation.id] = operation
                self.release[operation.id] = max(job.release, operation.ready_time)
                for link in operation.predecessors:
                    self.successors[link.operation_id].append((operation.id, link))
        on_machine: dict[str, list[Assignment]] = defaultdict(list)
        for a in assignments:
            on_machine[a.machine_id].append(a)
        self.sequences = {
            m: sorted(items, key=lambda a: (a.start, a.end, a.operation_id))
            for m, items in on_machine.items()
        }
        self.machine_start = {
            m.id: max(problem.now, m.state.available_from) for m in problem.machines
        }

    def option(self, op_id: str) -> Any:
        a = self.by_op[op_id]
        return next(o for o in self.ops[op_id].options if o.machine_id == a.machine_id)

    def run(self, op_id: str) -> float:
        option = self.option(op_id)
        return float(option.fixed_seconds + self.ops[op_id].quantity * option.unit_seconds)

    def setup(self, machine_id: str, prev: str | None, nxt: str) -> float:
        return self.setups.seconds(machine_id, prev, nxt)

    def gate_of(self, op_id: str) -> float:
        a = self.by_op[op_id]
        return a.setup_end if self.ops[op_id].setup_detached else a.start

    def earliest_start(self, op_id: str) -> float:
        """Lowest start allowed by the material gate (attached reading), pin and machine."""
        operation = self.ops[op_id]
        lowest = max(self.release[op_id], self.machine_start[self.by_op[op_id].machine_id])
        if operation.pin is not None and operation.pin.earliest_start is not None:
            lowest = max(lowest, operation.pin.earliest_start)
        unit = self.option(op_id).unit_seconds
        for link in operation.predecessors:
            pred = self.by_op.get(link.operation_id)
            if pred is None:
                continue
            base = self._transfer(link.operation_id, pred, link.overlap_quantity)
            lowest = max(lowest, base + link.min_lag)
            if link.overlap_quantity is not None:
                floor = pred.end + link.overlap_quantity * unit
                lowest = max(lowest, floor - self.run(op_id))
        return lowest

    def _transfer(self, op_id: str, a: Assignment, quantity: float | None) -> float:
        if quantity is None:
            return a.end
        option = self.option(op_id)
        return a.setup_end + option.fixed_seconds + quantity * option.unit_seconds

    def max_lags_hold(self, op_id: str, start: float, setup_end: float, end: float) -> bool:
        moved = Assignment(
            operation_id=op_id,
            machine_id=self.by_op[op_id].machine_id,
            start=start,
            setup_end=setup_end,
            end=end,
        )
        for succ, link in self.successors[op_id]:
            if link.max_lag is None or succ not in self.by_op:
                continue
            base = self._transfer(op_id, moved, link.overlap_quantity)
            if self.gate_of(succ) > base + link.max_lag + EPS:
                return False
        return True


def _movable_ops(problem: Problem, assignments: tuple[Assignment, ...]) -> list[str]:
    """Ops that could start earlier without delaying another op (see the block above)."""
    plan = _Plan(problem, assignments)
    found: list[str] = []
    for machine_id, sequence in plan.sequences.items():
        for k, a in enumerate(sequence):
            if plan.ops[a.operation_id].status.state == "in_progress":
                continue
            if _move_found(plan, machine_id, sequence, k):
                found.append(f"{a.operation_id} on {machine_id} can start before {a.start:g}")
    return found


def _move_found(plan: _Plan, machine_id: str, sequence: list[Assignment], k: int) -> bool:
    return any(_fits(plan, machine_id, sequence, k, j) for j in range(k + 1))


def _fits(plan: _Plan, machine_id: str, sequence: list[Assignment], k: int, j: int) -> bool:
    """Can sequence[k] go right after sequence[j - 1] (j == k: shift left in place)?"""
    o = sequence[k].operation_id
    prev = sequence[j - 1] if j > 0 else None
    gap_start = prev.end if prev is not None else plan.machine_start[machine_id]
    start = max(gap_start, plan.earliest_start(o))
    if start >= sequence[k].start - EPS:
        return False
    setup_end = start + plan.setup(machine_id, prev.operation_id if prev else None, o)
    end = setup_end + plan.run(o)
    if not plan.max_lags_hold(o, start, setup_end, end):
        return False
    if j == k:
        return True
    prev_id = prev.operation_id if prev is not None else None
    return _next_keeps_its_run(plan, machine_id, o, end, sequence[j], prev_id) and _removal_ok(
        plan, machine_id, sequence, k
    )


def _next_keeps_its_run(
    plan: _Plan, machine_id: str, o: str, end: float, nxt: Assignment, prev: str | None
) -> bool:
    """`nxt` keeps its exact assignment: o ends by its start and its setup does not change."""
    same_setup = abs(
        plan.setup(machine_id, o, nxt.operation_id) - plan.setup(machine_id, prev, nxt.operation_id)
    )
    return end <= nxt.start + EPS and same_setup <= EPS


def _removal_ok(plan: _Plan, machine_id: str, sequence: list[Assignment], k: int) -> bool:
    """o's old successor z keeps its exact assignment: its setup from w equals that from o."""
    if k + 1 >= len(sequence):
        return True
    o, w, z = (sequence[i].operation_id for i in (k, k - 1, k + 1))
    return abs(plan.setup(machine_id, w, z) - plan.setup(machine_id, o, z)) <= EPS


def _active_request(seed: int) -> SolveRequest:
    return without_features(random_request(seed), ACTIVE_STRIP)


# --- the active check itself (the helper must catch what it claims to catch) -------------------


def _gap_problem() -> Problem:
    """One machine, A-1 (4 s) and B-1 (3 s), both ready at 0."""
    return request_from(
        {
            "id": "gap",
            "jobs": [
                {"id": "A", "operations": [op("A-1", {"M1": 4.0})]},
                {"id": "B", "operations": [op("B-1", {"M1": 3.0})]},
            ],
            "machines": [machine("M1")],
            "work_centers": [{"id": "WC"}],
        }
    ).problem


def _assign(op_id: str, start: float, end: float) -> Assignment:
    return Assignment(operation_id=op_id, machine_id="M1", start=start, setup_end=start, end=end)


def test_active_helper_flags_an_op_that_fits_an_earlier_gap() -> None:
    problem = _gap_problem()
    # A-1 at [5, 9) leaves [0, 5) idle; B-1 (3 s) at [9, 12) fits the gap.
    plan = (_assign("A-1", 5.0, 9.0), _assign("B-1", 9.0, 12.0))
    assert any(f.startswith("B-1") for f in _movable_ops(problem, plan))
    assert any(f.startswith("A-1") for f in _movable_ops(problem, plan))


def test_active_helper_accepts_a_tight_plan() -> None:
    problem = _gap_problem()
    plan = (_assign("A-1", 0.0, 4.0), _assign("B-1", 4.0, 7.0))
    assert _movable_ops(problem, plan) == []


def test_active_helper_does_not_flag_an_op_wider_than_the_gap() -> None:
    problem = _gap_problem()
    # Gap [0, 2) is too narrow for either op; B-1 sits right after A-1.
    plan = (_assign("A-1", 2.0, 6.0), _assign("B-1", 6.0, 9.0))
    assert [f for f in _movable_ops(problem, plan) if f.startswith("B-1")] == []
    assert any(f.startswith("A-1") for f in _movable_ops(problem, plan))  # left shift


# --- registration ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_construction_solvers_are_registered_deterministic(name: str) -> None:
    info = {i.name: i for i in available()}
    assert name in info, sorted(info)
    entry = info[name]
    assert entry.family == "construction"
    assert entry.deterministic is True
    assert entry.module == MODULE
    assert get_solver(name).name == name


@pytest.mark.parametrize("name", NAMES)
def test_construction_unknown_rule_is_refused_by_name(name: str) -> None:
    result = _solve(name, load_request("pinedo_7_1_1"), rule="no_such_rule")
    assert result.schedule.status == "UNKNOWN"
    codes = [i.code for i in (*result.schedule.issues, *result.issues)]
    assert "invalid_method_param" in codes, codes


@pytest.mark.parametrize("name", NAMES)
def test_construction_default_rule_verifies_and_is_repeatable(name: str) -> None:
    request = load_request("pinedo_14_2_1")
    first = _run(name, request)
    second = _run(name, request)
    assert first.schedule.assignments == second.schedule.assignments


# --- gt_active: active schedules (REQ-097) -------------------------------------------------------


@pytest.mark.parametrize("rule", ["edd", "atcs"])
def test_gt_active_returns_active_schedules_on_generated_problems(rule: str) -> None:
    checked = 0
    for seed in ACTIVE_SEEDS:
        request = _active_request(seed)
        compiled = compile_request(request)
        if validate(request) or validate(compiled):
            continue
        result = _solve("gt_active", request, rule)
        if result.schedule.status not in ("FEASIBLE", "OPTIMAL"):
            assert result.schedule.issues, (seed, "UNKNOWN without a named issue")
            continue
        assert result.verified, (seed, result.issues)
        checked += 1
        movable = _movable_ops(compiled.problem, result.schedule.assignments)
        assert movable == [], f"seed {seed} rule {rule}: {movable[:5]}"
    assert checked >= MIN_ACTIVE_CHECKED, f"only {checked} problems solved"


def test_gt_active_on_7_1_1_is_active_with_every_rule() -> None:
    request = load_request("pinedo_7_1_1")
    problem = compile_request(request).problem
    for rule in _dispatch_rule_names():
        result = _run("gt_active", request, rule)
        assert _movable_ops(problem, result.schedule.assignments) == [], rule


def test_gt_active_first_choice_on_7_1_1_comes_from_the_root_branch() -> None:
    # Root of 7.1.1 (Pinedo p.185): t* = 4 (3-1 on M1), C = {1-1, 3-1} on M1, both at 0.
    request = load_request("pinedo_7_1_1")
    index = ProblemIndex.build(compile_request(request).problem)
    root = DispatchEngine(index, _ZeroRule(), mode="active").initial_state()
    branch = gt_branch(root)
    assert branch == (("1-1", "M1"), ("3-1", "M1"))
    first_on_m1: set[str] = set()
    for rule in _dispatch_rule_names():
        result = _run("gt_active", request, rule)
        first = _order(result, "M1")[0]
        assert (first, "M1") in branch, (rule, first)
        assert _by_op(result)[first].start == pytest.approx(0.0), rule
        first_on_m1.add(first)
    # spt takes 3-1 (4 s), edd/fifo break the all-equal tie by op id (1-1): both branches.
    assert {(f, "M1") for f in first_on_m1} == set(branch)


class _ZeroRule:
    def index(self, state: Any, op_id: str, machine_id: str, t: float, last: str | None) -> float:
        return 0.0


# --- forward loading (REQ-098) -------------------------------------------------------------------


def _forward_case() -> SolveRequest:
    """EDD priority A (due 10) > B (due 50) > C (due 60), all released at 0.

    A-1 on M2 [0, 3), so A-2 (M1, 5 s) is ready at 3. Forward loading in priority order:
    A-1 M2 [0, 3); A-2 M1 [3, 8); B-1 (M1, 8 s) does not fit M1's gap [0, 3), so [8, 16);
    C-1 (M1, 2 s, lowest priority) goes into the gap [0, 2). A non-delay EDD pass would
    instead start B-1 at 0 (the only ready op on M1 then) and push A-2 to 8.
    """
    problem = {
        "id": "forward",
        "jobs": [
            {
                "id": "A",
                "due": 10.0,
                "operations": [
                    op("A-1", {"M2": 3.0}),
                    op("A-2", {"M1": 5.0}, after=["A-1"]),
                ],
            },
            {"id": "B", "due": 50.0, "operations": [op("B-1", {"M1": 8.0})]},
            {"id": "C", "due": 60.0, "operations": [op("C-1", {"M1": 2.0})]},
        ],
        "machines": [machine("M1"), machine("M2")],
        "work_centers": [{"id": "WC"}],
    }
    return request_from(problem)


def test_forward_load_places_in_priority_order_with_gap_insertion() -> None:
    result = _run("forward_load", _forward_case(), "edd")
    starts = {op_id: a.start for op_id, a in _by_op(result).items()}
    assert starts == pytest.approx({"A-1": 0.0, "A-2": 3.0, "B-1": 8.0, "C-1": 0.0})
    assert _order(result, "M1") == ["C-1", "A-2", "B-1"]


def test_forward_load_starts_no_earlier_than_now() -> None:
    data = _forward_case().model_dump(mode="json")
    data["problem"]["now"] = 100.0
    result = _run("forward_load", SolveRequest.model_validate(data), "edd")
    assert min(a.start for a in result.schedule.assignments) >= 100.0 - EPS
    starts = {op_id: a.start - 100.0 for op_id, a in _by_op(result).items()}
    assert starts == pytest.approx({"A-1": 0.0, "A-2": 3.0, "B-1": 8.0, "C-1": 0.0})


@pytest.mark.parametrize("seed", range(20))
def test_forward_load_verifies_or_names_its_issue_on_generated_problems(seed: int) -> None:
    result = _solve("forward_load", random_request(seed), "edd")
    codes = {i.code for i in (*result.schedule.issues, *result.issues)}
    assert "solver_error" not in codes, (seed, codes)
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified, (seed, result.issues)
    else:
        assert codes, seed


# --- backward loading (REQ-098) ------------------------------------------------------------------


def _backward_case() -> SolveRequest:
    """One machine, three 10 s jobs due 10, 20, 30: no slack, so only 1, 2, 3 is on time.

    Jobs are listed 3, 2, 1 so neither list order nor op-id-reversed order gives it.
    """
    jobs = [
        {"id": str(j), "due": 10.0 * j, "operations": [op(f"{j}-1", {"M1": 10.0})]}
        for j in (3, 2, 1)
    ]
    problem = {
        "id": "backward",
        "jobs": jobs,
        "machines": [machine("M1")],
        "work_centers": [{"id": "WC"}],
    }
    profile = {"objective": {"mode": "lexicographic", "levels": [{"kind": "weighted_tardiness"}]}}
    return request_from(problem, profile=profile)


@pytest.mark.parametrize("rule", [None, "edd", "spt"])
def test_backward_load_sequences_by_latest_due_and_ends_each_job_at_its_due(
    rule: str | None,
) -> None:
    result = _run("backward_load", _backward_case(), rule)
    assert _order(result, "M1") == ["1-1", "2-1", "3-1"]
    ends = {op_id: a.end for op_id, a in _by_op(result).items()}
    assert ends == pytest.approx({"1-1": 10.0, "2-1": 20.0, "3-1": 30.0})
    assert result.schedule.primary_value == pytest.approx(0.0)


def test_backward_load_decodes_forward_from_now() -> None:
    data = _backward_case().model_dump(mode="json")
    data["problem"]["now"] = 5.0
    result = _run("backward_load", SolveRequest.model_validate(data))
    assert min(a.start for a in result.schedule.assignments) >= 5.0 - EPS
    assert _order(result, "M1") == ["1-1", "2-1", "3-1"]


@pytest.mark.parametrize("seed", range(20))
def test_backward_load_verifies_or_names_its_issue_on_generated_problems(seed: int) -> None:
    result = _solve("backward_load", random_request(seed))
    codes = {i.code for i in (*result.schedule.issues, *result.issues)}
    assert "solver_error" not in codes, (seed, codes)
    if result.schedule.status in ("FEASIBLE", "OPTIMAL"):
        assert result.verified, (seed, result.issues)
    else:
        assert codes, seed
