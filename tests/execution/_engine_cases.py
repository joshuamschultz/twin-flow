"""Hand-built v2 problems, schedules and a factor stub for the executor tests (T-173 to T-176).

Small problems are written out in full so every expected time in a test can be computed by
hand. `run_plan` executes a schedule with the contract of `test_fidelity.py`; `QuantityFactors`
is a `DistributionSource` that gives chosen run factors to the operations whose lot quantity
matches (`OpContext` carries no operation id, so a test marks the op to slow down with a
distinctive quantity and `unit_seconds` 0, which keeps its planned run independent of it).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from twinflow.execution import engine
from twinflow.execution.policy import BreakdownRule, Deterministic, Distribution, ScriptedOutage
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource, DistributionSource, OpContext
from twinflow.scheduling import apply_overrides, verify
from twinflow.scheduling.model import (
    Assignment,
    Calendar,
    CompiledProblem,
    Job,
    Machine,
    Operation,
    OperatorNeed,
    OperatorPool,
    Precedence,
    Problem,
    ProcessingOption,
    Schedule,
    SetupFunction,
    SolveRequest,
    WorkCenter,
)

SLOW = 7.0
"""Lot quantity that marks the operation `QuantityFactors` slows down."""

EPS = 1e-6
"""Time tolerance of every comparison (the verifier's TOLERANCE)."""


def option(machine_id: str, fixed: float, *, work_center_id: str | None = None) -> ProcessingOption:
    """An option whose run is exactly `fixed` seconds (unit seconds 0)."""
    return ProcessingOption(
        machine_id=machine_id,
        fixed_seconds=fixed,
        unit_seconds=0.0,
        preference_cost=0.0,
        work_center_id=work_center_id,
    )


def after(*operation_ids: str) -> tuple[Precedence, ...]:
    """End-to-gate links with min lag 0 from each predecessor."""
    return tuple(Precedence(operation_id=op_id) for op_id in operation_ids)


def need(pool_id: str, phase: str, quantity: int = 1) -> OperatorNeed:
    """Operators from `pool_id` held during `phase`."""
    return OperatorNeed.model_validate({"pool_id": pool_id, "quantity": quantity, "phase": phase})


def op(
    op_id: str,
    machine_id: str,
    fixed: float,
    *,
    work_center_id: str = "A",
    quantity: float = 1.0,
    **fields: Any,
) -> Operation:
    """A one-option operation on `machine_id` in `work_center_id`; extra fields as the model."""
    return Operation(
        id=op_id,
        work_center_id=work_center_id,
        quantity=quantity,
        options=(option(machine_id, fixed),),
        **fields,
    )


def job(job_id: str, *operations: Operation, release: float = 0.0) -> Job:
    """An open job of the given operations."""
    return Job(id=job_id, release=release, operations=operations)


def problem(
    jobs: Sequence[Job],
    machines: Sequence[Machine],
    *,
    calendars: Sequence[Calendar] = (),
    pools: Sequence[OperatorPool] = (),
    setup_functions: Sequence[SetupFunction] = (),
    center_setups: Mapping[str, str] | None = None,
    now: float = 0.0,
) -> Problem:
    """A problem whose work centers are those of its machines and operations."""
    centers = sorted(
        {m.work_center_id for m in machines}
        | {o.work_center_id for j in jobs for o in j.operations}
    )
    setups = center_setups or {}
    return Problem(
        id="hand-built",
        now=now,
        jobs=tuple(jobs),
        machines=tuple(machines),
        work_centers=tuple(WorkCenter(id=c, setup_function_id=setups.get(c)) for c in centers),
        calendars=tuple(calendars),
        operator_pools=tuple(pools),
        setup_functions=tuple(setup_functions),
    )


def compile_problem(value: Problem) -> CompiledProblem:
    """The compiled problem of a request without overrides."""
    return apply_overrides(SolveRequest(schema_version="2.0", problem=value))


def at(
    op_id: str,
    machine_id: str,
    start: float,
    setup_end: float,
    end: float,
    batch_id: str | None = None,
) -> Assignment:
    """One planned assignment."""
    return Assignment(
        operation_id=op_id,
        machine_id=machine_id,
        start=start,
        setup_end=setup_end,
        end=end,
        batch_id=batch_id,
    )


def plan(*assignments: Assignment) -> Schedule:
    """A hand-built schedule."""
    return Schedule(status="FEASIBLE", assignments=assignments, solver="test", elapsed_seconds=0.0)


def assert_verified(compiled: CompiledProblem, schedule: Schedule) -> None:
    """Precondition: the independent verifier accepts the hand-built plan."""
    result = verify(compiled, (), schedule)
    assert result.valid, [issue.message for issue in result.issues]


class QuantityFactors:
    """A deterministic source: run factor `run[quantity]` for ops of that lot size, else 1."""

    name = "test-quantity-factors"
    digest = "test-quantity-factors"

    def __init__(self, run: Mapping[float, float]) -> None:
        self._run = dict(run)

    def is_stochastic(self, shop: object) -> bool:
        """Every factor is a constant."""
        return False

    def run_factor(self, ctx: OpContext) -> Distribution:
        """The chosen factor for the op's quantity, else exactly 1."""
        return Deterministic(value=self._run.get(ctx.quantity, 1.0))

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """Setups run as planned."""
        return Deterministic(value=1.0)

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """No machine fails."""
        return None

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """No scripted outages."""
        return ()


def slow(factor: float) -> QuantityFactors:
    """Run factor `factor` for every op of quantity `SLOW`."""
    return QuantityFactors({SLOW: factor})


def run_plan(
    compiled: CompiledProblem, schedule: Schedule, source: DistributionSource | None = None
) -> Any:
    """Execute `schedule` under plan control (the `test_fidelity.py` contract)."""
    shop = build_shop(compiled)
    return engine.execute(
        shop,
        engine.PlanControl(schedule),
        draws=engine.RepDraws(seed=1, replication=0, source=source or DeterministicSource()),
        outages=(),
        limits=engine.Limits.for_shop(shop),
    )


def by_op(run: Any) -> dict[str, Any]:
    """Records by operation id; fails when an operation is recorded twice."""
    records = {r.operation_id: r for r in run.records}
    assert len(records) == len(run.records), "an operation was recorded twice"
    return records


def times(record: Any) -> tuple[float, float, float]:
    """(start, setup_end, end) of a record."""
    return record.start, record.setup_end, record.end


def assert_times(record: Any, start: float, setup_end: float, end: float) -> None:
    """The record's times equal the expected ones within `EPS`."""
    got = times(record)
    expected = (start, setup_end, end)
    assert all(abs(g - e) <= EPS for g, e in zip(got, expected, strict=True)), (
        f"{record.operation_id}: got {got}, expected {expected}"
    )


def assert_replays(run: Any, schedule: Schedule) -> None:
    """Every planned op ran on its machine at its planned times, and the run completed."""
    assert run.termination == "completed"
    records = by_op(run)
    assert set(records) == {a.operation_id for a in schedule.assignments}
    for a in schedule.assignments:
        assert records[a.operation_id].machine_id == a.machine_id, a.operation_id
        assert_times(records[a.operation_id], a.start, a.setup_end, a.end)


def need_span(phase: str, record: Any) -> tuple[float, float]:
    """The verifier's `_need_span` on a record: setup, run or both phases."""
    if phase == "setup":
        return record.start, record.setup_end
    if phase == "run":
        return record.setup_end, record.end
    return record.start, record.end


def peak_pool_usage(
    records: Iterable[Any], operations: Mapping[str, Operation], pool_id: str
) -> int:
    """Highest simultaneous operator count of `pool_id` over the records' need spans.

    Half-open spans; at equal times releases come before acquisitions (the verifier's sweep).
    """
    events: list[tuple[float, int, int]] = []
    for record in records:
        for n in operations[record.operation_id].operator_needs:
            start, end = need_span(n.phase, record)
            if n.pool_id == pool_id and end > start:
                events += [(end, 0, -n.quantity), (start, 1, n.quantity)]
    level = peak = 0
    for _, _, delta in sorted(events):
        level += delta
        peak = max(peak, level)
    return peak
