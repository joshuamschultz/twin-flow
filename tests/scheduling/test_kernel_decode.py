"""Decoder: machine choice + per-machine sequences -> schedule (T-122, COMP-004, D-071).

`decode(index, choice, sequences)` returns a `Decoded` or a named `ScheduleIssue`
(`sequence_cycle`, `max_lag_unmet`, ...). Each op is placed append-only after its machine
predecessor, as early as its gates allow, through the TF-002 timelines (calendars forbid
and pause, operators, batches joining only the machine's last batch, restart, pins,
in-progress first). `Decoded.start/setup_end/end(op)`, `Decoded.completion(job)` and
`Decoded.schedule(solver, status="FEASIBLE")` expose the result. Malformed input (an op
listed twice or not at all, a sequence on a machine other than the op's choice, a choice
that is not an eligible option) raises ValueError: it is a method bug, not a plan issue.

Generated-problem properties (200 seeds, decoder input from a verified baseline plan):

- every decoded schedule verifies. A semi-active decoder cannot always keep a `max_lag`:
  when a predecessor moves earlier and the successor's machine is still busy, the window
  closes. So a decode may return `max_lag_unmet`, only on a problem with max lags, and at
  most `MAX_LAG_MISSES` times (proposed deviation from the PLAN wording, see report);
- no op starts later than in the source (left shift). This holds only without operators,
  restart and batching: an earlier op can take an operator another op had, an earlier
  end can lengthen a later idle into a warm-up, and an earlier batch head can leave a
  member unable to join. The property runs on the seeds with those three stripped.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from generators import random_request, scale_request
from kernel_cases import (
    BOOK_16_4_1_COMPLETIONS,
    BOOK_16_4_1_SEQUENCES,
    BOOK_16_4_1_WEIGHTED_TARDINESS,
    OPTIMAL_7_1_1,
    compile_request,
    load_request,
    machine,
    machine_choice,
    machine_sequences,
    only_choice,
    op,
    pinedo_16_4_1,
    request_from,
    without_features,
)
from oracle import brute_force

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.model import (
    ObjectiveKind,
    Schedule,
    ScheduleIssue,
    SolveRequest,
    SolveResult,
)
from twinflow.scheduling.objectives import evaluate
from twinflow.scheduling.seam import Feature
from twinflow.scheduling.solver import problem_features, solve
from twinflow.scheduling.verifier import verify

SEEDS = range(200)
EPS = 1e-6
MAX_LAG_MISSES = 10
MIN_DECODED = 170
LEFT_SHIFT_BREAKERS = ("operators", "restart", "batching")


def _decode_request(
    request: SolveRequest, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> Decoded | ScheduleIssue:
    index = ProblemIndex.build(compile_request(request).problem)
    return decode(index, choice, sequences)


def _decoded(
    request: SolveRequest, choice: Mapping[str, str], sequences: Mapping[str, Sequence[str]]
) -> Decoded:
    result = _decode_request(request, choice, sequences)
    assert isinstance(result, Decoded), result
    return result


def _assert_verifies(request: SolveRequest, schedule: Schedule) -> None:
    compiled = compile_request(request)
    verdict = verify(compiled, request.overrides, schedule, request.previous_plan)
    assert verdict.valid, verdict.issues


# --- Pinedo examples -----------------------------------------------------------------------------


def test_decode_7_1_1_oracle_optimal_sequences_gives_makespan_28() -> None:
    request = load_request("pinedo_7_1_1")
    value, best = brute_force(request.problem, ObjectiveKind.MAKESPAN)
    sequences = machine_sequences(best)

    decoded = _decoded(request, only_choice(request), sequences)
    schedule = decoded.schedule("test")

    assert value == 28.0
    assert evaluate(compile_request(request), schedule)[ObjectiveKind.MAKESPAN] == 28.0
    _assert_verifies(request, schedule)


def test_decode_7_1_1_fixed_optimal_sequences_completions() -> None:
    request = load_request("pinedo_7_1_1")

    decoded = _decoded(request, only_choice(request), OPTIMAL_7_1_1)

    assert {j: decoded.completion(j) for j in ("1", "2", "3")} == {
        "1": 22.0,
        "2": 28.0,
        "3": 28.0,
    }
    assert decoded.start("2-1") == 0.0
    assert decoded.end("2-4") == 28.0


def test_decode_16_4_1_book_sequences_gives_book_completions_and_163_5() -> None:
    request = pinedo_16_4_1()
    choice = {o: m for m, seq in BOOK_16_4_1_SEQUENCES.items() for o in seq}

    decoded = _decoded(request, choice, BOOK_16_4_1_SEQUENCES)
    schedule = decoded.schedule("test")
    values = evaluate(compile_request(request), schedule)

    completions = {job: decoded.completion(job) for job in BOOK_16_4_1_COMPLETIONS}
    assert completions == pytest.approx(BOOK_16_4_1_COMPLETIONS)
    assert values[ObjectiveKind.WEIGHTED_TARDINESS] == pytest.approx(BOOK_16_4_1_WEIGHTED_TARDINESS)
    assert decoded.start("5-2") == 37.0 and decoded.setup_end("5-2") == 40.0  # detached setup
    _assert_verifies(request, schedule)


# --- named issues --------------------------------------------------------------------------------


def test_decode_cyclic_sequence_pair_returns_sequence_cycle() -> None:
    request = load_request("pinedo_7_1_1")
    sequences = dict(OPTIMAL_7_1_1)
    sequences["M1"] = ("2-2", "1-1", "3-1")  # 2-2 waits on 2-1, which M2 puts after 1-2
    sequences["M2"] = ("1-2", "2-1", "3-2")  # 1-2 waits on 1-1, which M1 puts after 2-2

    result = _decode_request(request, only_choice(request), sequences)

    assert isinstance(result, ScheduleIssue)
    assert result.code == "sequence_cycle"


def test_decode_unmeetable_max_lag_returns_max_lag_unmet() -> None:
    # b needs a1 done and runs 50 s on M2 before a2, yet a2 must start within 10 s of a1.
    jobs = [
        {
            "id": "A",
            "operations": [
                op("a1", {"M1": 5.0}),
                op("a2", {"M2": 5.0}, after=[{"operation_id": "a1", "max_lag": 10.0}]),
            ],
        },
        {"id": "B", "operations": [op("b", {"M2": 50.0}, after=["a1"])]},
    ]
    request = _request(jobs, [machine("M1"), machine("M2")])

    result = _decode_request(request, {"a1": "M1", "a2": "M2", "b": "M2"}, _seq(M1="a1", M2="b a2"))

    assert isinstance(result, ScheduleIssue)
    assert result.code == "max_lag_unmet"


@pytest.mark.parametrize(
    ("choice", "sequences"),
    [
        ({"a": "M1", "b": "M1"}, {"M1": ["a"]}),  # b in no sequence
        ({"a": "M1", "b": "M1"}, {"M1": ["a", "b", "a"]}),  # a twice
        ({"a": "M1", "b": "M1"}, {"M1": ["a"], "M2": ["b"]}),  # b sequenced off its choice
        ({"a": "M1", "b": "M3"}, {"M1": ["a"], "M3": ["b"]}),  # M3 is not an option of b
    ],
)
def test_decode_malformed_input_raises_value_error(
    choice: dict[str, str], sequences: dict[str, list[str]]
) -> None:
    jobs = [{"id": "J", "operations": [op("a", {"M1": 1.0}), op("b", {"M1": 1.0, "M2": 1.0})]}]
    request = _request(jobs, [machine("M1"), machine("M2"), machine("M3")])
    index = ProblemIndex.build(request.problem)

    with pytest.raises(ValueError):
        decode(index, choice, sequences)


# --- features honored ----------------------------------------------------------------------------


def _request(
    jobs: list[dict[str, Any]], machines: list[dict[str, Any]], **problem: Any
) -> SolveRequest:
    data = {"id": "case", "jobs": jobs, "machines": machines, "work_centers": [{"id": "WC"}]}
    return request_from(data | problem)


def _seq(**machines: str) -> dict[str, list[str]]:
    return {m: ops.split() for m, ops in machines.items()}


def _single(*ops: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": f"J-{o['id']}", "operations": [o]} for o in ops]


CAL = {"calendars": [{"id": "C", "intervals": [(0.0, 10.0), (20.0, 100.0)]}]}


def test_decode_calendar_forbid_moves_op_to_next_working_interval() -> None:
    request = _request(
        _single(op("a", {"M1": 8.0}), op("b", {"M1": 5.0})),
        [machine("M1", calendar_id="C")],
        **CAL,
    )

    decoded = _decoded(request, {"a": "M1", "b": "M1"}, _seq(M1="a b"))

    assert (decoded.start("b"), decoded.end("b")) == (20.0, 25.0)
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_calendar_pause_runs_across_the_break() -> None:
    request = _request(
        _single(op("a", {"M1": 8.0}), op("b", {"M1": 5.0}, shift_crossing="pause")),
        [machine("M1", calendar_id="C")],
        **CAL,
    )

    decoded = _decoded(request, {"a": "M1", "b": "M1"}, _seq(M1="a b"))

    assert (decoded.start("b"), decoded.end("b")) == (8.0, 23.0)
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_operator_pool_of_one_serializes_two_machines() -> None:
    need = [{"pool_id": "P", "quantity": 1, "phase": "both"}]
    request = _request(
        _single(
            op("a", {"M1": 10.0}, operator_needs=need), op("b", {"M2": 10.0}, operator_needs=need)
        ),
        [machine("M1"), machine("M2")],
        operator_pools=[{"id": "P", "size": 1}],
    )

    decoded = _decoded(request, {"a": "M1", "b": "M2"}, _seq(M1="a", M2="b"))

    assert sorted([decoded.start("a"), decoded.start("b")]) == [0.0, 10.0]
    _assert_verifies(request, decoded.schedule("test"))


def _batch_request(families: str) -> SolveRequest:
    ops = [op(f"x{k}", {"B": 10.0}, batch_family=f) for k, f in enumerate(families)]
    return _request(_single(*ops), [machine("B", batch={"capacity": 2.0, "unit": "pc"})])


def _batches(schedule: Schedule) -> dict[str, tuple[str | None, float]]:
    return {a.operation_id: (a.batch_id, a.start) for a in schedule.assignments}


def test_decode_batch_joins_consecutive_same_family_up_to_capacity() -> None:
    request = _batch_request("FFF")

    decoded = _decoded(request, dict.fromkeys(("x0", "x1", "x2"), "B"), _seq(B="x0 x1 x2"))
    got = _batches(decoded.schedule("test"))

    assert got["x0"][0] == got["x1"][0] != got["x2"][0]
    assert [got[x][1] for x in ("x0", "x1", "x2")] == [0.0, 0.0, 10.0]
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_batch_joins_only_the_last_batch_on_the_machine() -> None:
    request = _batch_request("FGF")

    decoded = _decoded(request, dict.fromkeys(("x0", "x1", "x2"), "B"), _seq(B="x0 x1 x2"))
    got = _batches(decoded.schedule("test"))

    assert len({got[x][0] for x in ("x0", "x1", "x2")}) == 3
    assert [got[x][1] for x in ("x0", "x1", "x2")] == [0.0, 10.0, 20.0]
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_restart_adds_warmup_after_long_idle() -> None:
    restart = {"idle_threshold_seconds": 5.0, "warmup_seconds": 3.0}
    jobs = [
        {"id": "A", "operations": [op("a", {"M1": 2.0})]},
        {"id": "B", "release": 20.0, "operations": [op("b", {"M1": 2.0})]},
    ]
    request = _request(jobs, [machine("M1", restart=restart)])

    decoded = _decoded(request, {"a": "M1", "b": "M1"}, _seq(M1="a b"))

    assert (decoded.start("b"), decoded.setup_end("b"), decoded.end("b")) == (20.0, 23.0, 25.0)
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_pin_earliest_start_delays_op() -> None:
    request = _request(
        _single(op("a", {"M1": 2.0}, pin={"earliest_start": 7.0}), op("b", {"M1": 2.0})),
        [machine("M1")],
    )

    decoded = _decoded(request, {"a": "M1", "b": "M1"}, _seq(M1="a b"))

    assert (decoded.start("a"), decoded.start("b")) == (7.0, 9.0)
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_in_progress_op_runs_from_now_for_remaining_seconds() -> None:
    status = {"state": "in_progress", "machine_id": "M1", "remaining_seconds": 6.0}
    request = _request(
        _single(op("a", {"M1": 20.0}, status=status), op("b", {"M1": 2.0})),
        [machine("M1")],
        now=100.0,
    )

    decoded = _decoded(request, {"a": "M1", "b": "M1"}, _seq(M1="a b"))

    assert (decoded.start("a"), decoded.setup_end("a"), decoded.end("a")) == (100.0, 100.0, 106.0)
    assert decoded.start("b") == 106.0
    _assert_verifies(request, decoded.schedule("test"))


def test_decode_schedule_carries_solver_and_status() -> None:
    request = load_request("pinedo_7_1_1")

    schedule = _decoded(request, only_choice(request), OPTIMAL_7_1_1).schedule("tabu")

    assert (schedule.solver, schedule.status) == ("tabu", "FEASIBLE")
    assert len(schedule.assignments) == 10


# --- generated problems --------------------------------------------------------------------------


@functools.cache
def _source(seed: int, strip: tuple[str, ...] = ()) -> tuple[SolveRequest, SolveResult]:
    request = random_request(seed)
    if strip:
        request = without_features(request, strip)
    return request, solve(request)


def _decode_source(seed: int, strip: tuple[str, ...] = ()) -> Decoded | ScheduleIssue | None:
    request, result = _source(seed, strip)
    if not result.verified:
        return None
    schedule = result.schedule
    return _decode_request(request, machine_choice(schedule), machine_sequences(schedule))


def test_decode_of_200_verified_baseline_plans_verifies() -> None:
    decoded_count, misses = 0, []
    for seed in SEEDS:
        request, _ = _source(seed)
        result = _decode_source(seed)
        if result is None:
            continue
        if isinstance(result, ScheduleIssue):
            features = problem_features(compile_request(request).problem)
            assert result.code == "max_lag_unmet", f"seed {seed}: {result}"
            assert Feature.MAX_LAGS in features, f"seed {seed}: max_lag_unmet without max lags"
            misses.append(seed)
            continue
        compiled = compile_request(request)
        schedule = result.schedule("decode")
        verdict = verify(compiled, request.overrides, schedule, request.previous_plan)
        assert verdict.valid, f"seed {seed}: {verdict.issues}"
        decoded_count += 1
    print(f"\ndecoded {decoded_count}, max_lag misses {misses}")
    assert len(misses) <= MAX_LAG_MISSES, misses
    assert decoded_count >= MIN_DECODED


def test_decode_left_shifts_200_baseline_plans_without_operators_restart_batching() -> None:
    checked = 0
    for seed in SEEDS:
        request, source = _source(seed, LEFT_SHIFT_BREAKERS)
        result = _decode_source(seed, LEFT_SHIFT_BREAKERS)
        if result is None or isinstance(result, ScheduleIssue):
            assert result is None or result.code == "max_lag_unmet", f"seed {seed}: {result}"
            continue
        _assert_verifies(request, result.schedule("decode"))
        later = [
            a.operation_id
            for a in source.schedule.assignments
            if result.start(a.operation_id) > a.start + EPS
        ]
        assert later == [], f"seed {seed}: ops start later than the source: {later}"
        checked += 1
    print(f"\nleft shift checked on {checked} seeds")
    assert checked >= MIN_DECODED


@pytest.mark.slow
def test_decode_600_by_8_is_under_three_seconds() -> None:
    request = scale_request(0)
    source = solve(request)
    assert source.verified, source.issues
    index = ProblemIndex.build(compile_request(request).problem)
    choice, sequences = machine_choice(source.schedule), machine_sequences(source.schedule)

    started = time.perf_counter()
    result = decode(index, choice, sequences)
    elapsed = time.perf_counter() - started

    assert isinstance(result, Decoded), result
    _assert_verifies(request, result.schedule("decode"))
    assert elapsed < 3.0, f"decode took {elapsed:.2f} s"
