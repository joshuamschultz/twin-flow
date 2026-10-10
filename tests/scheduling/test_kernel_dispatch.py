"""Dispatch engine and machine policy (T-124, COMP-007, REQ-090, REQ-091, D-071, D-072).

API under test (`twinflow.scheduling.kernel.dispatch`):

- `PriorityRule`: `index(state, op_id, machine_id, t, last_op_id) -> float` (higher goes
  first; `last_op_id` is the machine's last placed op or None) and an optional
  `prepare(index)` the engine calls once before the first `index` call.
- `DispatchEngine(index, rule, *, mode="nondelay" | "active", policy=PreferredUnlessLate())`
  with `run(deadline=None) -> Dispatched | ScheduleIssue`. `Dispatched` holds `choice`
  (op -> machine), `sequences` (machine -> op ids in order) and `decoded`, which is exactly
  `kernel.decode.decode(index, choice, sequences)` (D-071: the engine builds sequences, the
  decoder builds the schedule). A deadline already passed gives the issue `time_limit`.
- `DispatchEngine.initial_state() -> DispatchState`; `DispatchState.copy()` is independent;
  `DispatchState.commit(op_id, machine_id)` places one op (ValueError for an op that is not
  ready or a machine that is not an option); `gt_branch(state)` is the Giffler-Thompson
  conflict set (Pinedo Alg 7.1.3, p.183-184): t* = min earliest completion over ready
  (op, machine) pairs, i* its machine, C = ready ops eligible on i* whose earliest start on
  i* < t*, as (op_id, machine_id) pairs sorted by op id then machine id; () when done.
- `PreferredUnlessLate` (D-072, Pinedo p.523): an op goes to its lowest `preference_cost`
  option whose estimated finish `max(f_k, est(o)) + p_k(o)` meets `local_due(o)`; only
  when no lowest-cost option meets it, to the option with the earliest estimated finish
  (any cost). Ops without a due date: lowest-cost options only. Among accepted options of
  equal cost the engine's machine order decides (free time, then machine id) and the rule
  picks which op that machine takes.

`fifo` (registered in `algorithms/dispatch_rules.py`): earliest material release
(max(job release, op ready_time)) first, then earliest ready time (material gate from
predecessors), then op id.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Mapping
from typing import Any

import pytest
from generators import random_request
from kernel_cases import compile_request, load_request, machine, op, request_from, without_features

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.dispatch import (
    Dispatched,
    DispatchEngine,
    PreferredUnlessLate,
    gt_branch,
)
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import ScheduleIssue, SolveRequest, SolveResult
from twinflow.scheduling.registry import available
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate
from twinflow.scheduling.verifier import verify

EPS = 1e-6
NONDELAY_SEEDS = range(50)
MIN_NONDELAY_CHECKED = 40
PROPERTY_SEEDS = range(200)
MIN_FIFO_VERIFIED = 150
NONDELAY_STRIP = ("calendars", "pause", "operators", "batching", "restart", "max_lags")


# --- test rules ------------------------------------------------------------------------------


class TableRule:
    """Index from a table (default 0): lets a test fix which op a machine takes."""

    def __init__(self, values: Mapping[str, float] | None = None) -> None:
        self._values = dict(values or {})

    def index(
        self, state: object, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> float:
        return self._values.get(op_id, 0.0)


class RecordingRule:
    """Constant index; records every call and every `prepare`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float, str | None]] = []
        self.prepared: list[ProblemIndex] = []

    def prepare(self, index: ProblemIndex) -> None:
        self.prepared.append(index)

    def index(
        self, state: object, op_id: str, machine_id: str, t: float, last_op_id: str | None
    ) -> float:
        assert self.prepared, "index called before prepare"
        self.calls.append((op_id, machine_id, t, last_op_id))
        return 0.0


# --- builders --------------------------------------------------------------------------------


def _option(machine_id: str, run: float, pref: float) -> dict[str, Any]:
    return {
        "machine_id": machine_id,
        "fixed_seconds": run,
        "unit_seconds": 0.0,
        "preference_cost": pref,
    }


def _op_with(op_id: str, options: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    data = {
        "id": op_id,
        "work_center_id": "WC",
        "quantity": 1.0,
        "options": options,
        "predecessors": [],
    }
    return data | fields


def _job(job_id: str, ops: list[dict[str, Any]], **fields: Any) -> dict[str, Any]:
    return {"id": job_id, "operations": ops} | fields


def _problem(jobs: list[dict[str, Any]], machine_ids: list[str]) -> dict[str, Any]:
    return {
        "id": "dispatch-case",
        "jobs": jobs,
        "machines": [machine(m) for m in machine_ids],
        "work_centers": [{"id": "WC"}],
    }


def _index_of(request: SolveRequest) -> ProblemIndex:
    return ProblemIndex.build(compile_request(request).problem)


def _dispatch(problem: dict[str, Any], rule: Any, **kwargs: Any) -> Dispatched:
    result = DispatchEngine(_index_of(request_from(problem)), rule, **kwargs).run()
    assert isinstance(result, Dispatched), f"engine returned {result}"
    return result


def _sequence(dispatched: Dispatched, machine_id: str) -> list[str]:
    return list(dispatched.sequences.get(machine_id, ()))


# --- D-071: the engine's schedule is the decoder's -------------------------------------------


def test_dispatch_result_equals_decode_of_its_sequences() -> None:
    request = load_request("pinedo_7_1_1")
    index = _index_of(request)
    result = DispatchEngine(index, TableRule()).run()
    assert isinstance(result, Dispatched)
    again = decode(index, result.choice, result.sequences)
    assert isinstance(again, Decoded)
    assert result.decoded.starts == again.starts
    assert result.decoded.ends == again.ends
    assert result.decoded.setup_ends == again.setup_ends


def test_dispatch_schedule_on_7_1_1_verifies_in_both_modes() -> None:
    request = load_request("pinedo_7_1_1")
    compiled = compile_request(request)
    for mode in ("nondelay", "active"):
        result = DispatchEngine(ProblemIndex.build(compiled.problem), TableRule(), mode=mode).run()
        assert isinstance(result, Dispatched), mode
        schedule = result.decoded.schedule("dispatch-test")
        verdict = verify(compiled, request.overrides, schedule)
        assert verdict.valid, (mode, verdict.issues)


def test_dispatch_passed_deadline_returns_time_limit() -> None:
    index = _index_of(load_request("pinedo_7_1_1"))
    result = DispatchEngine(index, TableRule()).run(deadline=time.monotonic() - 1.0)
    assert isinstance(result, ScheduleIssue)
    assert result.code == "time_limit"


# --- rule protocol ---------------------------------------------------------------------------


def test_dispatch_rule_prepared_once_and_given_t_and_last_op() -> None:
    # One machine, runs A 3, B 5, C 2, constant index -> op id order A [0,3], B [3,8],
    # C [8,10]. Decisions happen at t = 0 (last None), t = 3 (last A), t = 8 (last B).
    jobs = [
        _job("A", [op("A-1", {"M1": 3.0})]),
        _job("B", [op("B-1", {"M1": 5.0})]),
        _job("C", [op("C-1", {"M1": 2.0})]),
    ]
    rule = RecordingRule()
    result = _dispatch(_problem(jobs, ["M1"]), rule)
    assert _sequence(result, "M1") == ["A-1", "B-1", "C-1"]
    assert len(rule.prepared) == 1
    allowed = {(0.0, None), (3.0, "A-1"), (8.0, "B-1")}
    assert {(t, last) for _, _, t, last in rule.calls} <= allowed
    at_three = {op_id for op_id, machine_id, t, _ in rule.calls if t == 3.0 and machine_id == "M1"}
    assert at_three == {"B-1", "C-1"}


def test_dispatch_index_ties_break_by_op_id() -> None:
    # Problem order B, C, A; equal index -> lowest op id first.
    jobs = [
        _job("B", [op("B-1", {"M1": 1.0})]),
        _job("C", [op("C-1", {"M1": 1.0})]),
        _job("A", [op("A-1", {"M1": 1.0})]),
    ]
    result = _dispatch(_problem(jobs, ["M1"]), TableRule())
    assert _sequence(result, "M1") == ["A-1", "B-1", "C-1"]


def test_dispatch_higher_index_goes_first() -> None:
    jobs = [
        _job("A", [op("A-1", {"M1": 1.0})]),
        _job("B", [op("B-1", {"M1": 1.0})]),
        _job("C", [op("C-1", {"M1": 1.0})]),
    ]
    rule = TableRule({"A-1": 1.0, "B-1": 3.0, "C-1": 2.0})
    result = _dispatch(_problem(jobs, ["M1"]), rule)
    assert _sequence(result, "M1") == ["B-1", "C-1", "A-1"]


# --- preferred unless late (D-072, Pinedo p.523) ---------------------------------------------


@pytest.mark.parametrize(("slow", "fast"), [("M1", "M2"), ("M2", "M1")])
@pytest.mark.parametrize(("due", "expected"), [(20.0, "slow"), (6.0, "fast"), (2.0, "fast")])
def test_dispatch_preferred_machine_unless_late(
    slow: str, fast: str, due: float, expected: str
) -> None:
    # X-1: pref 0 on `slow` (run 10), pref 2 on `fast` (run 4); single op so dloc = due.
    # due 20: slow finishes 10 <= 20 -> slow. due 6: slow 10 > 6, fast 4 <= 6 -> fast.
    # due 2: neither meets -> earliest finish (fast, 4). Machine names are swapped in the
    # second case so the slow machine is also the one the engine looks at first.
    options = [_option(slow, 10.0, 0.0), _option(fast, 4.0, 2.0)]
    jobs = [_job("X", [_op_with("X-1", sorted(options, key=lambda o: o["machine_id"]))], due=due)]
    result = _dispatch(_problem(jobs, ["M1", "M2"]), TableRule())
    want = slow if expected == "slow" else fast
    assert result.choice["X-1"] == want
    assert result.decoded.start("X-1") == 0.0


def test_dispatch_late_preferred_goes_to_earliest_finish_not_next_cheapest() -> None:
    # X-1 (due 9): M1 pref 0 run 10, M2 pref 1 run 8, M3 pref 2 run 4. The pref-0 option
    # misses (10 > 9), so the earliest finish wins: M3 (4), not the next-cheapest M2 (8).
    options = [_option("M1", 10.0, 0.0), _option("M2", 8.0, 1.0), _option("M3", 4.0, 2.0)]
    jobs = [_job("X", [_op_with("X-1", options)], due=9.0)]
    result = _dispatch(_problem(jobs, ["M1", "M2", "M3"]), TableRule())
    assert result.choice["X-1"] == "M3"


def test_dispatch_busy_preferred_machine_sends_due_op_to_alternate() -> None:
    # Y-1 only on M1 (run 50, no due) is ranked first. X-1 (due 30): M1 pref 0 run 10,
    # M2 pref 2 run 10. t=0, M1 (machine id first): Y-1 wins -> M1 [0, 50], f_M1 = 50.
    # M2 at 0: pref-0 finish max(50, 0) + 10 = 60 > 30, M2 finish 10 <= 30 -> X-1 on M2.
    jobs = [
        _job("Y", [_op_with("Y-1", [_option("M1", 50.0, 0.0)])]),
        _job(
            "X", [_op_with("X-1", [_option("M1", 10.0, 0.0), _option("M2", 10.0, 2.0)])], due=30.0
        ),
    ]
    result = _dispatch(_problem(jobs, ["M1", "M2"]), TableRule({"Y-1": 2.0, "X-1": 1.0}))
    assert result.choice == {"Y-1": "M1", "X-1": "M2"}
    assert result.decoded.start("X-1") == 0.0


def test_dispatch_op_without_due_waits_for_preferred_machine() -> None:
    # As above but X-1 has no due date: preferred only, so it waits for M1 (start 50)
    # while M2 (pref 2, run 1) stays idle: X-1 is not accepted there.
    jobs = [
        _job("Y", [_op_with("Y-1", [_option("M1", 50.0, 0.0)])]),
        _job("X", [_op_with("X-1", [_option("M1", 10.0, 0.0), _option("M2", 1.0, 2.0)])]),
    ]
    result = _dispatch(_problem(jobs, ["M1", "M2"]), TableRule({"Y-1": 2.0, "X-1": 1.0}))
    assert result.choice == {"Y-1": "M1", "X-1": "M1"}
    assert result.decoded.start("X-1") == 50.0
    assert _sequence(result, "M2") == []


@pytest.mark.parametrize(("top", "other"), [("A-1", "B-1"), ("B-1", "A-1")])
def test_dispatch_equal_preferences_pick_by_rule_then_machine_id(top: str, other: str) -> None:
    # A-1 and B-1 both on M1 and M2 at pref 0, run 5. At t=0 M1 (lower id) chooses first
    # and takes the rule's top op; M2 takes the other.
    both = [_option("M1", 5.0, 0.0), _option("M2", 5.0, 0.0)]
    jobs = [_job("A", [_op_with("A-1", both)]), _job("B", [_op_with("B-1", both)])]
    result = _dispatch(_problem(jobs, ["M1", "M2"]), TableRule({top: 1.0}))
    assert result.choice == {top: "M1", other: "M2"}


def test_dispatch_single_op_equal_preferences_takes_lower_machine_id() -> None:
    both = [_option("M1", 5.0, 0.0), _option("M2", 5.0, 0.0)]
    result = _dispatch(_problem([_job("A", [_op_with("A-1", both)])], ["M1", "M2"]), TableRule())
    assert result.choice == {"A-1": "M1"}


def test_dispatch_explicit_policy_equals_default() -> None:
    index = _index_of(load_request("pinedo_7_1_1"))
    default = DispatchEngine(index, TableRule()).run()
    explicit = DispatchEngine(index, TableRule(), policy=PreferredUnlessLate()).run()
    assert isinstance(default, Dispatched) and isinstance(explicit, Dispatched)
    assert default.decoded.starts == explicit.decoded.starts


# --- non-delay property ----------------------------------------------------------------------


def _nondelay_request(seed: int) -> SolveRequest:
    """Seed request without calendars, pause, operators, batching, restart, max lags, the
    frozen zone, and without due dates (so the policy accepts exactly the lowest-cost
    options and the check below can know which ops were accepted where)."""
    stripped = without_features(random_request(seed), (*NONDELAY_STRIP, "frozen_zone"))
    data = stripped.model_dump()
    for job in data["problem"]["jobs"]:
        job["due"] = None
    return SolveRequest.model_validate(data)


def _preferred(index: ProblemIndex, p: int) -> set[str]:
    options = index.ops[p].options
    lowest = min(o.preference_cost for o in options)
    return {o.machine_id for o in options if o.preference_cost == lowest}


def _ready_bound(index: ProblemIndex, decoded: Decoded, p: int) -> float:
    """An upper bound on when op p could first start: release, pred end + min_lag (an
    overestimate with transfer lots), pin earliest start."""
    data = index.ops[p]
    ready = data.release
    for link in data.predecessors:
        ready = max(ready, decoded.ends[link.pred_pos] + link.min_lag)
    if data.pin is not None and data.pin.earliest_start is not None:
        ready = max(ready, data.pin.earliest_start)
    return ready


def _idle_gaps(index: ProblemIndex, decoded: Decoded) -> list[tuple[str, float, float]]:
    """(machine, a, b) for every interval a machine sits idle before its next op."""
    gaps: list[tuple[str, float, float]] = []
    for m in index.machines:
        on_m = sorted(
            (p for p in range(len(index.ops)) if decoded.options[p].machine_id == m.id),
            key=lambda p: (decoded.starts[p], decoded.ends[p]),
        )
        cursor = max(index.now, m.available_from)
        for p in on_m:
            if decoded.starts[p] > cursor + EPS:
                gaps.append((m.id, cursor, decoded.starts[p]))
            cursor = max(cursor, decoded.ends[p])
    return gaps


def _nondelay_violations(index: ProblemIndex, decoded: Decoded) -> list[str]:
    """Ops accepted on an idle machine, ready by the gap's start, yet started later."""
    found: list[str] = []
    for machine_id, a, b in _idle_gaps(index, decoded):
        for p, data in enumerate(index.ops):
            if data.in_progress is not None or machine_id not in _preferred(index, p):
                continue
            if data.pin is not None and data.pin.latest_start is not None:
                continue
            if decoded.starts[p] > a + EPS and _ready_bound(index, decoded, p) <= a + EPS:
                found.append(f"{data.id} ready by {a:g} but {machine_id} idle until {b:g}")
    return found


def test_dispatch_nondelay_never_idles_with_accepted_ready_op() -> None:
    checked = 0
    for seed in NONDELAY_SEEDS:
        request = _nondelay_request(seed)
        compiled = compile_request(request)
        if validate(request) or validate(compiled):
            continue
        index = ProblemIndex.build(compiled.problem)
        result = DispatchEngine(index, TableRule(), mode="nondelay").run()
        if isinstance(result, ScheduleIssue):
            continue
        checked += 1
        decoded = result.decoded
        assert _nondelay_violations(index, decoded) == [], f"seed {seed}"
        for p, data in enumerate(index.ops):
            chosen = decoded.options[p].machine_id
            assert chosen in _preferred(index, p), f"seed {seed}: {data.id} on {chosen}"
        again = decode(index, result.choice, result.sequences)
        assert isinstance(again, Decoded) and again.starts == decoded.starts, f"seed {seed}"
    assert checked >= MIN_NONDELAY_CHECKED, f"only {checked} problems dispatched"


# --- Giffler-Thompson branch and state copies --------------------------------------------------

# Fixture 7.1.1 (Pinedo p.185), all at t = 0:
#   job 1: 1-1 M1 10, 1-2 M2 8, 1-3 M3 4;  job 2: 2-1 M2 8, 2-2 M1 3, 2-3 M4 5, 2-4 M3 6;
#   job 3: 3-1 M1 4, 3-2 M2 7, 3-3 M4 3.
# Root: ready = 1-1 (M1, completes 0+10 = 10), 2-1 (M2, 0+8 = 8), 3-1 (M1, 0+4 = 4).
#   t* = 4 (3-1), i* = M1; ops on M1 with earliest start < 4: 1-1 (0), 3-1 (0).
ROOT_BRANCH = (("1-1", "M1"), ("3-1", "M1"))
# After 3-1 on M1 [0, 4]: ready = 1-1 (M1 from 4, completes 14), 2-1 (M2, 8),
#   3-2 (M2 from 4, completes 11). t* = 8 (2-1), i* = M2; on M2 earliest start < 8:
#   2-1 (0), 3-2 (4).
AFTER_3_1 = (("2-1", "M2"), ("3-2", "M2"))
# After 1-1 on M1 [0, 10] instead: ready = 2-1 (M2, 8), 3-1 (M1 from 10, 14),
#   1-2 (M2 from 10, 18). t* = 8, i* = M2; on M2 earliest start < 8: 2-1 only (1-2 at 10).
AFTER_1_1 = (("2-1", "M2"),)


def _state_7_1_1() -> Any:
    index = _index_of(load_request("pinedo_7_1_1"))
    return DispatchEngine(index, TableRule(), mode="active").initial_state()


def test_gt_branch_at_root_of_7_1_1() -> None:
    assert gt_branch(_state_7_1_1()) == ROOT_BRANCH


def test_gt_branch_after_commits_on_7_1_1() -> None:
    first = _state_7_1_1()
    first.commit("3-1", "M1")
    assert gt_branch(first) == AFTER_3_1
    second = _state_7_1_1()
    second.commit("1-1", "M1")
    assert gt_branch(second) == AFTER_1_1


def test_dispatch_state_copy_is_independent() -> None:
    root = _state_7_1_1()
    child = root.copy()
    child.commit("3-1", "M1")
    assert gt_branch(root) == ROOT_BRANCH
    assert gt_branch(child) == AFTER_3_1
    root.commit("1-1", "M1")
    assert gt_branch(child) == AFTER_3_1
    assert gt_branch(root) == AFTER_1_1


def test_gt_branch_rollout_commits_every_op_then_empties() -> None:
    state = _state_7_1_1()
    committed = 0
    while branch := gt_branch(state):
        state.commit(*branch[0])
        committed += 1
        assert committed <= 10, "rollout did not terminate"
    assert committed == 10


@pytest.mark.parametrize(("op_id", "machine_id"), [("1-2", "M2"), ("1-1", "M2"), ("9-9", "M1")])
def test_dispatch_state_commit_rejects_unready_or_ineligible(op_id: str, machine_id: str) -> None:
    # 1-2 waits on 1-1; M2 is not an option of 1-1; 9-9 does not exist.
    with pytest.raises(ValueError):
        _state_7_1_1().commit(op_id, machine_id)


# --- fifo ------------------------------------------------------------------------------------


def _fifo(problem: dict[str, Any]) -> SolveResult:
    result = solve(request_from(problem), solver=get_solver("fifo"))
    assert result.verified, result.issues
    return result


def _machine_order(result: SolveResult, machine_id: str) -> list[str]:
    on_m = [a for a in result.schedule.assignments if a.machine_id == machine_id]
    return [a.operation_id for a in sorted(on_m, key=lambda a: (a.start, a.operation_id))]


def test_fifo_registered_as_deterministic_dispatch_with_every_feature() -> None:
    info = {i.name: i for i in available()}["fifo"]
    assert info.family == "dispatch"
    assert info.deterministic is True
    assert info.capabilities == frozenset(Feature)
    assert info.module == "twinflow.scheduling.algorithms.dispatch_rules"


def test_fifo_orders_by_material_release_then_ready_time() -> None:
    # M1 is blocked by J0-1 [0, 20] (the only op ready at 0 on M1). At t = 20 the queue is
    #   JA-2: release 0, ready 15 (JA-1 on M2 [0, 15]);
    #   JB-1: release 5, ready 5;
    #   JC-2: release 0, ready 3 (JC-1 on M3 [0, 3]).
    # Release first: {JA-2, JC-2} (0) before JB-1 (5); ready breaks the tie: JC-2 (3) before
    # JA-2 (15). Order JC-2, JA-2, JB-1 (op id order would be JA-2, JB-1, JC-2; ready only
    # would be JC-2, JB-1, JA-2).
    jobs = [
        _job("J0", [op("J0-1", {"M1": 20.0})]),
        _job("JA", [op("JA-1", {"M2": 15.0}), op("JA-2", {"M1": 1.0}, after=["JA-1"])]),
        _job("JB", [op("JB-1", {"M1": 1.0})], release=5.0),
        _job("JC", [op("JC-1", {"M3": 3.0}), op("JC-2", {"M1": 1.0}, after=["JC-1"])]),
    ]
    result = _fifo(_problem(jobs, ["M1", "M2", "M3"]))
    assert _machine_order(result, "M1") == ["J0-1", "JC-2", "JA-2", "JB-1"]


def test_fifo_uses_op_ready_time_in_material_release() -> None:
    # Material release = max(job release, op ready_time). M1 blocked by Z-1 [0, 10]; at 10:
    # A-1 ready_time 6, B-1 job release 2 -> B-1 (2) before A-1 (6).
    jobs = [
        _job("Z", [op("Z-1", {"M1": 10.0})]),
        _job("A", [op("A-1", {"M1": 1.0}, ready_time=6.0)]),
        _job("B", [op("B-1", {"M1": 1.0})], release=2.0),
    ]
    result = _fifo(_problem(jobs, ["M1"]))
    assert _machine_order(result, "M1") == ["Z-1", "B-1", "A-1"]


@functools.cache
def _fifo_seed(seed: int) -> SolveResult:
    return solve(random_request(seed), solver=get_solver("fifo"))


def test_fifo_property_verifies_or_names_issue() -> None:
    verified = 0
    for seed in PROPERTY_SEEDS:
        result = _fifo_seed(seed)
        status = result.schedule.status
        assert status in ("FEASIBLE", "UNKNOWN"), f"seed {seed}: {status}"
        if status == "FEASIBLE":
            assert result.verified, f"seed {seed}: {result.issues}"
            assert result.schedule.primary_value is not None, f"seed {seed}"
            verified += 1
            continue
        codes = [issue.code for issue in result.issues]
        assert codes, f"seed {seed}: UNKNOWN without an issue"
        assert "solver_error" not in codes, f"seed {seed}: fifo raised"
        assert "unsupported_feature" not in codes, f"seed {seed}: {codes}"
    assert verified >= MIN_FIFO_VERIFIED, f"only {verified} fifo schedules verified"
