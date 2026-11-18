"""Hand-built v2 problems and plans for the executor unit tests (T-177 to T-180).

The engine tests build tiny problems whose every time is computed by hand, write the plan
assignment by assignment, and (where the plan should replay exactly) check it with the
independent verifier first, so a failing engine test never hides a bad fixture.

`run_plan` imports `twinflow.execution.engine` lazily: before the engine exists every test
fails at that call with `ModuleNotFoundError`, after its fixture and preconditions passed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from twinflow.execution.policy import (
    BreakdownRule,
    Deterministic,
    Distribution,
    ScriptedOutage,
)
from twinflow.execution.shop import build_shop
from twinflow.execution.sources import DeterministicSource, DistributionSource, OpContext
from twinflow.scheduling import (
    Assignment,
    CompiledProblem,
    Schedule,
    SolveRequest,
    apply_overrides,
    verify,
)

Data = dict[str, Any]
EXACT = 1e-6
"""Replay tolerance (REQ-131, the verifier's TOLERANCE)."""


def option(machine_id: str, fixed: float, unit: float = 0.0) -> Data:
    """A processing option with no preference cost."""
    return {
        "machine_id": machine_id,
        "fixed_seconds": fixed,
        "unit_seconds": unit,
        "preference_cost": 0.0,
    }


def op(op_id: str, center: str, options: list[Data], quantity: float = 1.0, **extra: Any) -> Data:
    """An operation as request data."""
    return {
        "id": op_id,
        "work_center_id": center,
        "quantity": quantity,
        "options": options,
        **extra,
    }


def override(name: str, kind: str, **fields: Any) -> Data:
    """A hard override as request data."""
    return {
        "id": name,
        "kind": kind,
        "hardness": "hard",
        "fact_ref": "hand-built",
        "reason": "engine test",
        **fields,
    }


def request(
    jobs: list[Data],
    machines: list[Data],
    work_centers: list[Data],
    *,
    now: float = 0.0,
    horizon_end: float | None = None,
    calendars: Sequence[Data] = (),
    pools: Sequence[Data] = (),
    setup_functions: Sequence[Data] = (),
    overrides: Sequence[Data] = (),
) -> SolveRequest:
    """A v2 request with the default profile."""
    problem: Data = {
        "id": "hand",
        "now": now,
        "horizon_end": horizon_end,
        "jobs": jobs,
        "machines": machines,
        "work_centers": work_centers,
        "calendars": list(calendars),
        "operator_pools": list(pools),
        "setup_functions": list(setup_functions),
    }
    data = {"schema_version": "2.0", "problem": problem, "overrides": list(overrides)}
    return SolveRequest.model_validate(data)


def a(
    op_id: str,
    machine_id: str,
    start: float,
    end: float,
    setup_end: float | None = None,
    batch_id: str | None = None,
) -> Assignment:
    """One assignment; `setup_end` defaults to `start` (no setup)."""
    return Assignment(
        operation_id=op_id,
        machine_id=machine_id,
        start=start,
        setup_end=start if setup_end is None else setup_end,
        end=end,
        batch_id=batch_id,
    )


def schedule(assignments: Sequence[Assignment]) -> Schedule:
    """A hand-made schedule (no primary value, so the verifier checks feasibility only)."""
    return Schedule(
        status="FEASIBLE", assignments=tuple(assignments), solver="hand", elapsed_seconds=0.0
    )


def hand_plan(
    req: SolveRequest, assignments: Sequence[Assignment]
) -> tuple[CompiledProblem, Schedule]:
    """Compile `req` and wrap the assignments, without verification."""
    return apply_overrides(req), schedule(assignments)


def verified_plan(
    req: SolveRequest, assignments: Sequence[Assignment]
) -> tuple[CompiledProblem, Schedule]:
    """Compile `req` and assert the hand-made plan verifies (test precondition)."""
    compiled, plan = hand_plan(req, assignments)
    assert not compiled.override_issues, compiled.override_issues
    result = verify(compiled, req.overrides, plan, req.previous_plan)
    assert result.valid, [issue.message for issue in result.issues]
    return compiled, plan


def run_plan(
    compiled: CompiledProblem,
    plan: Schedule,
    *,
    source: DistributionSource | None = None,
    seed: int = 1,
    replication: int = 0,
    outages: Sequence[ScriptedOutage] = (),
) -> Any:
    """Execute `plan` under plan control with the executor contract of test_fidelity.py."""
    from twinflow.execution.engine import Limits, PlanControl, RepDraws, execute

    shop = build_shop(compiled)
    return execute(
        shop,
        PlanControl(plan),
        draws=RepDraws(seed=seed, replication=replication, source=source or DeterministicSource()),
        outages=tuple(outages),
        limits=Limits.for_shop(shop),
    )


def records(run: Any) -> dict[str, Any]:
    """Records by operation id, asserting each operation is recorded once."""
    found = {r.operation_id: r for r in run.records}
    assert len(found) == len(run.records), "an operation recorded twice"
    return found


def assert_replays(run: Any, plan: Schedule) -> None:
    """Every planned operation ran on its machine at its planned start, setup end and end."""
    assert run.termination == "completed"
    found = records(run)
    assert set(found) == {x.operation_id for x in plan.assignments}
    for x in plan.assignments:
        r = found[x.operation_id]
        assert r.machine_id == x.machine_id, x.operation_id
        for name in ("start", "setup_end", "end"):
            assert abs(getattr(r, name) - getattr(x, name)) <= EXACT, (x.operation_id, name)


def planned_run(compiled: CompiledProblem, op_id: str, machine_id: str) -> float:
    """fixed + quantity x unit of the op's option on `machine_id` (the verifier's r)."""
    for job in compiled.problem.jobs:
        for operation in job.operations:
            if operation.id == op_id:
                chosen = next(o for o in operation.options if o.machine_id == machine_id)
                return chosen.fixed_seconds + operation.quantity * chosen.unit_seconds
    raise KeyError(op_id)


class StubSource:
    """A deterministic source with fixed factors per machine or per family (test seam).

    `run` and `setup` map a machine id or a family to a constant factor (machine wins);
    anything else gets 1. No breakdowns; `scripted` are its outages.
    """

    name = "stub"
    digest = "stub"

    def __init__(
        self,
        run: Mapping[str, float] | None = None,
        setup: Mapping[str, float] | None = None,
        scripted: Sequence[ScriptedOutage] = (),
    ) -> None:
        self._run = dict(run or {})
        self._setup = dict(setup or {})
        self._scripted = tuple(scripted)

    def is_stochastic(self, shop: object) -> bool:
        """Never stochastic: every factor is a constant."""
        return False

    def _factor(self, table: Mapping[str, float], ctx: OpContext) -> Distribution:
        value = table.get(ctx.machine_id, table.get(ctx.family or "", 1.0))
        return Deterministic(value=value)

    def run_factor(self, ctx: OpContext) -> Distribution:
        """The machine's (or family's) run factor, else 1."""
        return self._factor(self._run, ctx)

    def setup_factor(self, ctx: OpContext) -> Distribution:
        """The machine's (or family's) setup factor, else 1."""
        return self._factor(self._setup, ctx)

    def breakdown(self, machine_id: str, work_center_id: str) -> BreakdownRule | None:
        """No machine fails."""
        return None

    def outages(self) -> tuple[ScriptedOutage, ...]:
        """The scripted outages given at construction."""
        return self._scripted
