"""Behavior tests for `apply_overrides` and the frozen zone (T-093, T-095; COMP-003).

Each override kind and hardness from the SDD override table is compiled on a small
2-job, 2-machine problem and the compiled effect is asserted on the public
`CompiledProblem`. Requirements: REQ-065 (overrides), REQ-066 (frozen zone). Decision: D-066.

Choices these tests pin where the SDD is silent:
- split children are numbered from 1: `J1~1`, `J1~2`, ops `A1~1`, `A1~2`;
- a soft override adds `penalty` to `preference_cost`;
- issue messages name every override id involved; issue paths start with "overrides"
  (for `frozen_requires_previous_plan`, with "profile");
- a frozen op is released only by a lock on it or a hard exclude of its previous machine.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError

from twinflow.scheduling.model import (
    CompiledProblem,
    ExcludeMachineOverride,
    ExpediteOverride,
    FrozenZone,
    HoldOverride,
    Job,
    LockOverride,
    Machine,
    Operation,
    OperationStatus,
    Override,
    Pin,
    Precedence,
    PreferMachineOverride,
    PreviousAssignment,
    PreviousPlan,
    PriorityOverride,
    Problem,
    ProcessingOption,
    SchedulingProfile,
    SolveRequest,
    SplitOverride,
    WorkCenter,
)
from twinflow.scheduling.overrides import apply_overrides
from twinflow.scheduling.solver import solve
from twinflow.scheduling.validator import validate

FACT = {"fact_ref": "erp:order/42", "reason": "customer call"}


# --- builders ------------------------------------------------------------------------------


def _options(machine_ids: tuple[str, ...] = ("M1", "M2")) -> tuple[ProcessingOption, ...]:
    return tuple(
        ProcessingOption(machine_id=m, fixed_seconds=10.0, unit_seconds=1.0, preference_cost=1.0)
        for m in machine_ids
    )


def _op(op_id: str, *, after: str | None = None, **fields: Any) -> Operation:
    fields.setdefault("options", _options())
    predecessors = (Precedence(operation_id=after, min_lag=5.0),) if after else ()
    return Operation(
        id=op_id, work_center_id="WC1", quantity=10.0, predecessors=predecessors, **fields
    )


def _problem(*, b1_options: tuple[str, ...] = ("M1", "M2"), **j1_fields: Any) -> Problem:
    """J1 = A1 -> A2 (class 1, release 0, due 100); J2 = B1 (class 2, due 300)."""
    j1_fields = {"release": 0.0, "due": 100.0, **j1_fields}
    j1 = Job(id="J1", operations=(_op("A1"), _op("A2", after="A1")), **j1_fields)
    j2 = Job(
        id="J2",
        weight_class=2,
        due=300.0,
        operations=(_op("B1", options=_options(b1_options)),),
    )
    machines = tuple(Machine(id=m, work_center_id="WC1") for m in ("M1", "M2"))
    return Problem(id="P", jobs=(j1, j2), machines=machines, work_centers=(WorkCenter(id="WC1"),))


def _request(
    *overrides: Any,
    problem: Problem | None = None,
    profile: SchedulingProfile | None = None,
    previous_plan: PreviousPlan | None = None,
) -> SolveRequest:
    return SolveRequest(
        schema_version="2.0",
        problem=problem or _problem(),
        profile=profile or SchedulingProfile(),
        overrides=tuple(overrides),
        previous_plan=previous_plan,
    )


def _job(compiled: CompiledProblem, job_id: str) -> Job:
    return next(job for job in compiled.problem.jobs if job.id == job_id)


def _op_of(compiled: CompiledProblem, op_id: str) -> Operation:
    ops = (op for job in compiled.problem.jobs for op in job.operations)
    return next(op for op in ops if op.id == op_id)


def _costs(operation: Operation) -> dict[str, float]:
    return {option.machine_id: option.preference_cost for option in operation.options}


def _issues(compiled: CompiledProblem, code: str) -> list[Any]:
    return [issue for issue in compiled.override_issues if issue.code == code]


# --- no overrides and effective weights ----------------------------------------------------


def test_apply_overrides_no_overrides_keeps_problem_and_resolves_class_weights() -> None:
    request = _request()

    compiled = apply_overrides(request)

    assert compiled.profile == request.profile
    assert compiled.held_job_ids == ()
    assert compiled.released_from_frozen == ()
    assert compiled.split_map == {}
    assert compiled.override_issues == ()
    assert _job(compiled, "J1").weight_override == 1.0
    assert _job(compiled, "J2").weight_override == 2.0
    assert _job(compiled, "J1").operations == request.problem.jobs[0].operations


def test_apply_overrides_custom_class_weights_written_to_every_job() -> None:
    profile = SchedulingProfile(class_weights={1: 1.5, 2: 3.0, 4: 10.0})

    compiled = apply_overrides(_request(profile=profile))

    assert _job(compiled, "J1").weight_override == 1.5
    assert _job(compiled, "J2").weight_override == 3.0


def test_apply_overrides_explicit_job_weight_override_is_kept() -> None:
    compiled = apply_overrides(_request(problem=_problem(weight_override=7.0)))

    assert _job(compiled, "J1").weight_override == 7.0


# --- lock ----------------------------------------------------------------------------------


def test_lock_with_machine_and_start_pins_exact_start() -> None:
    lock = LockOverride(
        id="o1", hardness="hard", operation_id="A1", machine_id="M2", start=50.0, **FACT
    )

    compiled = apply_overrides(_request(lock))

    assert _op_of(compiled, "A1").pin == Pin(
        machine_id="M2", earliest_start=50.0, latest_start=50.0
    )
    assert compiled.override_issues == ()


def test_lock_without_start_pins_machine_only() -> None:
    lock = LockOverride(id="o1", hardness="hard", operation_id="A1", machine_id="M2", **FACT)

    compiled = apply_overrides(_request(lock))

    assert _op_of(compiled, "A1").pin == Pin(machine_id="M2")


def test_lock_start_only_pins_window_without_machine() -> None:
    lock = LockOverride(id="o1", hardness="hard", operation_id="A1", start=20.0, **FACT)

    compiled = apply_overrides(_request(lock))

    assert _op_of(compiled, "A1").pin == Pin(earliest_start=20.0, latest_start=20.0)


# --- expedite and priority -----------------------------------------------------------------


@pytest.mark.parametrize(("given_due", "expected_due"), [(50.0, 50.0), (200.0, 100.0)])
def test_expedite_sets_class_four_and_due_is_min_of_due_and_given(
    given_due: float, expected_due: float
) -> None:
    expedite = ExpediteOverride(id="o1", hardness="hard", job_id="J1", due=given_due, **FACT)

    compiled = apply_overrides(_request(expedite))

    job = _job(compiled, "J1")
    assert job.weight_class == 4
    assert job.weight_override == 4.0
    assert job.due == expected_due


def test_expedite_without_due_keeps_due_and_uses_profile_class_four_weight() -> None:
    profile = SchedulingProfile(class_weights={1: 1.0, 2: 2.0, 4: 9.0})
    expedite = ExpediteOverride(id="o1", hardness="hard", job_id="J1", **FACT)

    compiled = apply_overrides(_request(expedite, profile=profile))

    assert _job(compiled, "J1").due == 100.0
    assert _job(compiled, "J1").weight_override == 9.0


def test_expedite_due_on_job_without_due_sets_given_due() -> None:
    expedite = ExpediteOverride(id="o1", hardness="hard", job_id="J1", due=60.0, **FACT)

    compiled = apply_overrides(_request(expedite, problem=_problem(due=None)))

    assert _job(compiled, "J1").due == 60.0


def test_priority_weight_class_sets_class_and_resolved_weight() -> None:
    profile = SchedulingProfile(class_weights={1: 1.0, 2: 5.0, 4: 8.0})
    priority = PriorityOverride(id="o1", hardness="hard", job_id="J1", weight_class=2, **FACT)

    compiled = apply_overrides(_request(priority, profile=profile))

    assert _job(compiled, "J1").weight_class == 2
    assert _job(compiled, "J1").weight_override == 5.0


def test_priority_weight_sets_explicit_weight_override() -> None:
    priority = PriorityOverride(id="o1", hardness="hard", job_id="J2", weight=3.5, **FACT)

    compiled = apply_overrides(_request(priority))

    assert _job(compiled, "J2").weight_override == 3.5


# --- exclude_machine and prefer_machine ----------------------------------------------------


def test_exclude_hard_on_operation_removes_that_option_only() -> None:
    exclude = ExcludeMachineOverride(
        id="o1", hardness="hard", operation_id="A1", machine_id="M1", **FACT
    )

    compiled = apply_overrides(_request(exclude))

    assert [o.machine_id for o in _op_of(compiled, "A1").options] == ["M2"]
    assert [o.machine_id for o in _op_of(compiled, "A2").options] == ["M1", "M2"]


def test_exclude_hard_on_job_removes_option_from_every_job_operation() -> None:
    exclude = ExcludeMachineOverride(id="o1", hardness="hard", job_id="J1", machine_id="M1", **FACT)

    compiled = apply_overrides(_request(exclude))

    for op_id in ("A1", "A2"):
        assert [o.machine_id for o in _op_of(compiled, op_id).options] == ["M2"]
    assert [o.machine_id for o in _op_of(compiled, "B1").options] == ["M1", "M2"]


def test_exclude_soft_adds_penalty_to_that_option() -> None:
    exclude = ExcludeMachineOverride(
        id="o1", hardness="soft", operation_id="A1", machine_id="M1", penalty=40.0, **FACT
    )

    compiled = apply_overrides(_request(exclude))

    assert _costs(_op_of(compiled, "A1")) == {"M1": 41.0, "M2": 1.0}


def test_prefer_hard_keeps_only_that_option() -> None:
    prefer = PreferMachineOverride(
        id="o1", hardness="hard", operation_id="A2", machine_id="M2", **FACT
    )

    compiled = apply_overrides(_request(prefer))

    assert [o.machine_id for o in _op_of(compiled, "A2").options] == ["M2"]
    assert [o.machine_id for o in _op_of(compiled, "A1").options] == ["M1", "M2"]


def test_prefer_soft_adds_penalty_to_all_other_options() -> None:
    prefer = PreferMachineOverride(
        id="o1", hardness="soft", job_id="J1", machine_id="M2", penalty=15.0, **FACT
    )

    compiled = apply_overrides(_request(prefer))

    for op_id in ("A1", "A2"):
        assert _costs(_op_of(compiled, op_id)) == {"M1": 16.0, "M2": 1.0}


# --- split ---------------------------------------------------------------------------------


def test_split_half_half_creates_child_jobs_with_scaled_ops_and_remapped_precedence() -> None:
    split = SplitOverride(id="o1", hardness="hard", job_id="J1", fractions=(0.5, 0.5), **FACT)

    compiled = apply_overrides(_request(split))

    job_ids = {job.id for job in compiled.problem.jobs}
    assert job_ids == {"J1~1", "J1~2", "J2"}
    assert compiled.split_map == {
        "J1": ("J1~1", "J1~2"),
        "A1": ("A1~1", "A1~2"),
        "A2": ("A2~1", "A2~2"),
    }
    for k in ("1", "2"):
        child = _job(compiled, f"J1~{k}")
        assert [op.id for op in child.operations] == [f"A1~{k}", f"A2~{k}"]
        assert [op.quantity for op in child.operations] == [5.0, 5.0]
        assert child.operations[1].predecessors == (
            Precedence(operation_id=f"A1~{k}", min_lag=5.0),
        )
        assert (child.release, child.due, child.weight_override) == (0.0, 100.0, 1.0)


def test_split_uneven_fractions_scale_quantities() -> None:
    split = SplitOverride(id="o1", hardness="hard", job_id="J2", fractions=(0.25, 0.75), **FACT)

    compiled = apply_overrides(_request(split))

    assert _op_of(compiled, "B1~1").quantity == pytest.approx(2.5)
    assert _op_of(compiled, "B1~2").quantity == pytest.approx(7.5)


def test_split_scales_the_transfer_lot_with_the_fraction() -> None:
    """A transfer lot is a quantity of the split job, so it scales like the lot itself."""
    a1 = _op("A1")
    a2 = _op("A2").model_copy(
        update={"predecessors": (Precedence(operation_id="A1", overlap_quantity=10.0),)}
    )
    problem = _problem().model_copy(
        update={"jobs": (Job(id="J1", operations=(a1, a2)), _problem().jobs[1])}
    )
    split = SplitOverride(id="o1", hardness="hard", job_id="J1", fractions=(0.25, 0.75), **FACT)

    compiled = apply_overrides(_request(split, problem=problem))

    assert _op_of(compiled, "A2~1").predecessors[0].overlap_quantity == pytest.approx(2.5)
    assert _op_of(compiled, "A2~2").predecessors[0].overlap_quantity == pytest.approx(7.5)


def _cross_job_problem(**link: float | None) -> Problem:
    """`_problem` with B1 (job J2) waiting on A2 (job J1) through `link`."""
    base = _problem()
    b1 = _op("B1").model_copy(update={"predecessors": (Precedence(operation_id="A2", **link),)})
    j2 = base.jobs[1].model_copy(update={"operations": (b1,)})
    return base.model_copy(update={"jobs": (base.jobs[0], j2)})


def _split(job_id: str, *fractions: float, override_id: str = "o1") -> SplitOverride:
    return SplitOverride(
        id=override_id, hardness="hard", job_id=job_id, fractions=fractions, **FACT
    )


def test_split_remaps_a_link_from_another_job_to_every_child() -> None:
    problem = _cross_job_problem(min_lag=2.0, max_lag=50.0)

    compiled = apply_overrides(_request(_split("J1", 0.5, 0.5), problem=problem))

    assert compiled.override_issues == ()
    assert _op_of(compiled, "B1").predecessors == tuple(
        Precedence(operation_id=f"A2~{k}", min_lag=2.0, max_lag=50.0) for k in (1, 2)
    )


@pytest.mark.parametrize("ids", [("o1", "o2"), ("o2", "o1")])
def test_splitting_both_linked_jobs_remaps_every_child_link(ids: tuple[str, str]) -> None:
    splits = (
        _split("J1", 0.5, 0.5, override_id=ids[0]),
        _split("J2", 0.25, 0.75, override_id=ids[1]),
    )

    compiled = apply_overrides(_request(*splits, problem=_cross_job_problem()))

    assert compiled.override_issues == ()
    for k in (1, 2):
        links = _op_of(compiled, f"B1~{k}").predecessors
        assert [link.operation_id for link in links] == ["A2~1", "A2~2"]


# --- transfer-lot links from another job into a split job (T-115, REQ-124, D-084) ----------

LAGS = {"min_lag": 5.0, "max_lag": 50.0}


def _overlap_split_request() -> SolveRequest:
    """B1 (J2) waits on A2 (J1) with lags 5..50 and a transfer lot of 10; J1 split 1:3."""
    problem = _cross_job_problem(**LAGS, overlap_quantity=10.0)
    return _request(_split("J1", 0.25, 0.75), problem=problem)


def _link_fields(
    links: tuple[Precedence, ...],
) -> list[tuple[str, float, float | None, float | None]]:
    return [(lk.operation_id, lk.min_lag, lk.max_lag, lk.overlap_quantity) for lk in links]


def test_split_remaps_an_overlap_link_from_another_job_scaled_by_each_child_fraction() -> None:
    compiled = apply_overrides(_overlap_split_request())

    assert compiled.override_issues == ()
    assert _link_fields(_op_of(compiled, "B1").predecessors) == [
        ("A2~1", 5.0, 50.0, pytest.approx(2.5)),
        ("A2~2", 5.0, 50.0, pytest.approx(7.5)),
    ]


def test_split_with_an_overlap_link_from_another_job_is_applied_not_refused() -> None:
    compiled = apply_overrides(_overlap_split_request())

    assert "split_overlap_link" not in {issue.code for issue in compiled.override_issues}
    assert [job.id for job in compiled.problem.jobs] == ["J1~1", "J1~2", "J2"]


def test_split_keeps_a_cross_job_link_without_overlap_without_overlap() -> None:
    base = _cross_job_problem()
    links = (
        Precedence(operation_id="A1", min_lag=1.0),
        Precedence(operation_id="A2", overlap_quantity=10.0),
    )
    b1 = base.jobs[1].operations[0].model_copy(update={"predecessors": links})
    problem = base.model_copy(
        update={"jobs": (base.jobs[0], base.jobs[1].model_copy(update={"operations": (b1,)}))}
    )

    compiled = apply_overrides(_request(_split("J1", 0.25, 0.75), problem=problem))

    assert compiled.override_issues == ()
    assert _link_fields(_op_of(compiled, "B1").predecessors) == [
        ("A1~1", 1.0, None, None),
        ("A1~2", 1.0, None, None),
        ("A2~1", 0.0, None, pytest.approx(2.5)),
        ("A2~2", 0.0, None, pytest.approx(7.5)),
    ]


@pytest.mark.parametrize("ids", [("o1", "o2"), ("o2", "o1")])
def test_splitting_both_jobs_scales_the_overlap_by_the_predecessor_fraction(
    ids: tuple[str, str],
) -> None:
    problem = _cross_job_problem(**LAGS, overlap_quantity=10.0)
    splits = (
        _split("J1", 0.25, 0.75, override_id=ids[0]),
        _split("J2", 0.5, 0.5, override_id=ids[1]),
    )

    compiled = apply_overrides(_request(*splits, problem=problem))

    assert compiled.override_issues == ()
    for k in (1, 2):
        assert _link_fields(_op_of(compiled, f"B1~{k}").predecessors) == [
            ("A2~1", 5.0, 50.0, pytest.approx(2.5)),
            ("A2~2", 5.0, 50.0, pytest.approx(7.5)),
        ]


def test_split_with_a_cross_job_overlap_link_compiles_to_a_valid_problem() -> None:
    compiled = apply_overrides(_overlap_split_request())

    assert validate(compiled) == []


def test_split_with_a_cross_job_overlap_link_solves_to_a_verified_schedule() -> None:
    result = solve(_overlap_split_request())

    assert result.verified, result.issues
    assert "split_overlap_link" not in {issue.code for issue in result.issues}


@pytest.mark.parametrize("seed", range(40))
def test_split_jobs_with_cross_job_links_compile_to_a_problem_the_baseline_verifies(
    seed: int,
) -> None:
    rng = np.random.default_rng(seed)
    min_lag, spread = float(rng.integers(0, 20)), float(rng.integers(0, 200))
    overlap = {"overlap_quantity": float(rng.integers(1, 11))} if seed % 2 else {}
    # A max_lag window fanned out to several split children of a transfer-lot predecessor
    # can have no schedule at all (seed 29), so the transfer-lot seeds leave it open.
    max_lag = None if overlap else min_lag + spread
    problem = _cross_job_problem(min_lag=min_lag, max_lag=max_lag, **overlap)
    splits = [
        _split(job_id, *_fractions(rng), override_id=f"s{job_id}")
        for job_id in ("J1", "J2")
        if rng.random() < 0.7
    ]

    result = solve(_request(*splits, problem=problem))

    assert result.verified, (seed, result.issues)


def _fractions(rng: np.random.Generator) -> tuple[float, ...]:
    parts = rng.integers(1, 5, size=int(rng.integers(2, 4))).astype(float)
    return tuple(float(part) for part in parts / parts.sum())


@pytest.mark.parametrize(
    ("problem_end", "profile_end", "expected"),
    [
        (None, None, None),
        (80.0, None, 80.0),
        (None, 60.0, 60.0),
        (80.0, 50.0, 50.0),
        (40.0, 50.0, 40.0),
    ],
)
def test_compiled_horizon_is_the_earlier_of_problem_and_profile_horizon(
    problem_end: float | None, profile_end: float | None, expected: float | None
) -> None:
    problem = _problem().model_copy(update={"horizon_end": problem_end})
    profile = SchedulingProfile(horizon_end=profile_end)

    compiled = apply_overrides(_request(problem=problem, profile=profile))

    assert compiled.problem.horizon_end == expected


# --- hold ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("release", "until", "expected"), [(0.0, 80.0, 80.0), (90.0, 80.0, 90.0)])
def test_hold_until_sets_release_to_max_of_release_and_until(
    release: float, until: float, expected: float
) -> None:
    hold = HoldOverride(id="o1", hardness="hard", job_id="J1", until=until, **FACT)

    compiled = apply_overrides(_request(hold, problem=_problem(release=release)))

    assert _job(compiled, "J1").release == expected
    assert compiled.held_job_ids == ()


def test_hold_without_until_removes_job_and_lists_it_as_held() -> None:
    hold = HoldOverride(id="o1", hardness="hard", job_id="J1", **FACT)

    compiled = apply_overrides(_request(hold))

    assert [job.id for job in compiled.problem.jobs] == ["J2"]
    assert compiled.held_job_ids == ("J1",)


# --- model-level rejection (REQ-065) -------------------------------------------------------

_OVERRIDE = TypeAdapter(Override)


@pytest.mark.parametrize("field", ["fact_ref", "reason"])
def test_override_with_empty_fact_ref_or_reason_is_rejected(field: str) -> None:
    raw = {"kind": "hold", "id": "o1", "hardness": "hard", "job_id": "J1", **FACT, field: ""}

    with pytest.raises(ValidationError, match=field):
        _OVERRIDE.validate_python(raw)


@pytest.mark.parametrize(
    "raw",
    [
        {"kind": "lock", "operation_id": "A1", "machine_id": "M1"},
        {"kind": "expedite", "job_id": "J1"},
        {"kind": "priority", "job_id": "J1", "weight_class": 2},
        {"kind": "split", "job_id": "J1", "fractions": [0.5, 0.5]},
        {"kind": "hold", "job_id": "J1"},
    ],
    ids=lambda raw: raw["kind"],
)
def test_soft_hardness_rejected_for_hard_only_kinds(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="hardness"):
        _OVERRIDE.validate_python({**raw, "id": "o1", "hardness": "soft", **FACT})


@pytest.mark.parametrize("kind", ["exclude_machine", "prefer_machine"])
def test_unknown_hardness_rejected_for_machine_kinds(kind: str) -> None:
    raw = {"kind": kind, "id": "o1", "hardness": "medium", "job_id": "J1", "machine_id": "M1"}

    with pytest.raises(ValidationError, match="hardness"):
        _OVERRIDE.validate_python({**raw, **FACT})


# --- conflicts and unknown targets ---------------------------------------------------------


def test_exclude_the_only_option_is_an_override_conflict() -> None:
    exclude = ExcludeMachineOverride(
        id="ex1", hardness="hard", operation_id="B1", machine_id="M1", **FACT
    )

    compiled = apply_overrides(_request(exclude, problem=_problem(b1_options=("M1",))))

    conflicts = _issues(compiled, "override_conflict")
    assert len(conflicts) == 1
    assert "ex1" in conflicts[0].message
    assert conflicts[0].path.startswith("overrides")


def test_lock_on_excluded_machine_is_a_conflict_naming_both_ids() -> None:
    exclude = ExcludeMachineOverride(
        id="ex1", hardness="hard", operation_id="A1", machine_id="M1", **FACT
    )
    lock = LockOverride(id="lk1", hardness="hard", operation_id="A1", machine_id="M1", **FACT)

    compiled = apply_overrides(_request(exclude, lock))

    conflicts = _issues(compiled, "override_conflict")
    assert len(conflicts) == 1
    assert "ex1" in conflicts[0].message
    assert "lk1" in conflicts[0].message


def test_two_locks_on_one_operation_are_a_conflict_naming_both_ids() -> None:
    first = LockOverride(id="lk1", hardness="hard", operation_id="A1", machine_id="M1", **FACT)
    second = LockOverride(id="lk2", hardness="hard", operation_id="A1", machine_id="M2", **FACT)

    compiled = apply_overrides(_request(first, second))

    conflicts = _issues(compiled, "override_conflict")
    assert len(conflicts) == 1
    assert "lk1" in conflicts[0].message
    assert "lk2" in conflicts[0].message


@pytest.mark.parametrize(
    "override",
    [
        HoldOverride(id="bad", hardness="hard", job_id="NOPE", **FACT),
        ExpediteOverride(id="bad", hardness="hard", job_id="NOPE", **FACT),
        LockOverride(id="bad", hardness="hard", operation_id="NOPE", machine_id="M1", **FACT),
        LockOverride(id="bad", hardness="hard", operation_id="A1", machine_id="M9", **FACT),
        ExcludeMachineOverride(
            id="bad", hardness="hard", operation_id="NOPE", machine_id="M1", **FACT
        ),
        PreferMachineOverride(id="bad", hardness="soft", job_id="J1", machine_id="M9", **FACT),
    ],
    ids=["hold-job", "expedite-job", "lock-op", "lock-machine", "exclude-op", "prefer-machine"],
)
def test_unknown_target_is_reported_and_leaves_problem_unchanged(override: Any) -> None:
    request = _request(override)

    compiled = apply_overrides(request)

    unknown = _issues(compiled, "override_unknown_target")
    assert len(unknown) == 1
    assert "bad" in unknown[0].message
    assert unknown[0].path.startswith("overrides")
    assert compiled.problem == apply_overrides(_request()).problem


# --- purity and determinism (REQ-065) ------------------------------------------------------


def _mixed_overrides() -> list[Any]:
    """One non-conflicting override of every kind; hold and split both target J1."""
    return [
        HoldOverride(id="h1", hardness="hard", job_id="J1", until=40.0, **FACT),
        SplitOverride(id="s1", hardness="hard", job_id="J1", fractions=(0.5, 0.5), **FACT),
        PriorityOverride(id="p1", hardness="hard", job_id="J2", weight=3.0, **FACT),
        ExpediteOverride(id="e1", hardness="hard", job_id="J2", due=70.0, **FACT),
        ExcludeMachineOverride(
            id="x1", hardness="soft", operation_id="B1", machine_id="M1", penalty=2.0, **FACT
        ),
        ExcludeMachineOverride(
            id="x2", hardness="soft", operation_id="B1", machine_id="M2", penalty=3.0, **FACT
        ),
        PreferMachineOverride(
            id="f1", hardness="soft", operation_id="B1", machine_id="M2", penalty=5.0, **FACT
        ),
        LockOverride(id="l1", hardness="hard", operation_id="B1", start=45.0, **FACT),
    ]


def test_apply_overrides_is_pure_request_unchanged() -> None:
    request = _request(*_mixed_overrides())
    snapshot = request.model_dump()
    twin = SolveRequest.model_validate(snapshot)

    apply_overrides(request)

    assert request == twin
    assert request.model_dump() == snapshot


def test_apply_overrides_same_input_gives_same_output() -> None:
    assert apply_overrides(_request(*_mixed_overrides())) == apply_overrides(
        _request(*_mixed_overrides())
    )


def test_apply_overrides_shuffled_override_lists_compile_identically() -> None:
    # Assumption (D-066): the compile step sorts overrides by kind in the fixed order
    # hold, split, priority/expedite, exclude, prefer, lock, then by override id, so the
    # order of the request list never changes the result. No conflicts here, so no issue
    # path (which may carry a list index) can differ between shuffles.
    overrides = _mixed_overrides()
    expected = apply_overrides(_request(*overrides))
    rng = np.random.default_rng(20261010)

    for _ in range(20):
        shuffled = [overrides[i] for i in rng.permutation(len(overrides))]
        assert apply_overrides(_request(*shuffled)) == expected

    assert expected.override_issues == ()
    assert _costs(_op_of(expected, "B1")) == {"M1": 8.0, "M2": 4.0}
    assert _op_of(expected, "B1").pin == Pin(earliest_start=45.0, latest_start=45.0)
    # hold runs before split, so both children inherit the held release
    assert {_job(expected, c).release for c in ("J1~1", "J1~2")} == {40.0}
    j2 = _job(expected, "J2")
    # expedite and priority touch different fields, so their relative order is moot
    assert (j2.weight_class, j2.weight_override, j2.due) == (4, 3.0, 70.0)


# --- frozen zone (T-095, REQ-066) ----------------------------------------------------------

_FROZEN = SchedulingProfile(frozen_zone=FrozenZone(until=100.0, tolerance=5.0))


def _previous(*rows: tuple[str, str, float]) -> PreviousPlan:
    assignments = tuple(
        PreviousAssignment(operation_id=op, machine_id=m, start=start) for op, m, start in rows
    )
    return PreviousPlan(plan_id="plan-1", problem_digest="digest-1", assignments=assignments)


def _frozen_request(*overrides: Any, problem: Problem | None = None) -> SolveRequest:
    plan = _previous(("A1", "M2", 30.0), ("A2", "M1", 100.0), ("B1", "M1", 99.0))
    return _request(*overrides, problem=problem, profile=_FROZEN, previous_plan=plan)


def test_frozen_op_before_until_pinned_to_previous_machine_within_tolerance() -> None:
    compiled = apply_overrides(_frozen_request())

    assert _op_of(compiled, "A1").pin == Pin(
        machine_id="M2", earliest_start=25.0, latest_start=35.0
    )
    assert _op_of(compiled, "B1").pin == Pin(
        machine_id="M1", earliest_start=94.0, latest_start=104.0
    )
    assert compiled.released_from_frozen == ()
    assert compiled.override_issues == ()


def test_frozen_op_starting_at_until_is_free() -> None:
    compiled = apply_overrides(_frozen_request())

    assert _op_of(compiled, "A2").pin is None


def test_frozen_zone_zero_tolerance_pins_exact_previous_start() -> None:
    profile = SchedulingProfile(frozen_zone=FrozenZone(until=100.0, tolerance=0.0))
    request = _request(profile=profile, previous_plan=_previous(("A1", "M2", 30.0)))

    compiled = apply_overrides(request)

    assert _op_of(compiled, "A1").pin == Pin(
        machine_id="M2", earliest_start=30.0, latest_start=30.0
    )


def test_frozen_zone_ignores_done_operations() -> None:
    done = OperationStatus(state="done")
    problem = _problem()
    j1 = problem.jobs[0]
    a1_done = j1.operations[0].model_copy(update={"status": done})
    j1 = j1.model_copy(update={"operations": (a1_done, j1.operations[1])})
    problem = problem.model_copy(update={"jobs": (j1, problem.jobs[1])})

    compiled = apply_overrides(_frozen_request(problem=problem))

    assert _op_of(compiled, "A1").pin is None
    assert _op_of(compiled, "B1").pin is not None


def test_previous_plan_without_frozen_zone_pins_nothing() -> None:
    request = _request(previous_plan=_previous(("A1", "M2", 30.0)))

    compiled = apply_overrides(request)

    assert _op_of(compiled, "A1").pin is None


def test_lock_on_frozen_op_wins_and_op_is_released_from_frozen() -> None:
    lock = LockOverride(id="lk1", hardness="hard", operation_id="A1", machine_id="M1", **FACT)

    compiled = apply_overrides(_frozen_request(lock))

    assert _op_of(compiled, "A1").pin == Pin(machine_id="M1")
    assert compiled.released_from_frozen == ("A1",)
    assert _op_of(compiled, "B1").pin == Pin(
        machine_id="M1", earliest_start=94.0, latest_start=104.0
    )


def test_hard_exclude_of_frozen_machine_wins_and_op_is_released_from_frozen() -> None:
    exclude = ExcludeMachineOverride(
        id="ex1", hardness="hard", operation_id="A1", machine_id="M2", **FACT
    )

    compiled = apply_overrides(_frozen_request(exclude))

    a1 = _op_of(compiled, "A1")
    assert a1.pin is None
    assert [o.machine_id for o in a1.options] == ["M1"]
    assert compiled.released_from_frozen == ("A1",)
    assert compiled.override_issues == ()


def test_frozen_zone_without_previous_plan_is_an_issue_and_pins_nothing() -> None:
    compiled = apply_overrides(_request(profile=_FROZEN))

    issues = _issues(compiled, "frozen_requires_previous_plan")
    assert len(issues) == 1
    assert issues[0].path.startswith("profile")
    assert all(_op_of(compiled, op).pin is None for op in ("A1", "A2", "B1"))
