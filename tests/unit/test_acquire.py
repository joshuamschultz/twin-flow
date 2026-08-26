"""Unit tests for COMP-001 RunContext and COMP-002 ResourceAcquirer (T-004, red).

RunContext (engine/clock.py) owns the SimPy Environment for one replication.
ResourceAcquirer (engine/acquire.py) is the ONLY path to a dual machine+operator
acquire and is the deadlock guard (D-045, structure.md "Acquisition order is
global and fixed: machine, then operator"):

  * acquires machine THEN operator in one fixed global order, never operator-first;
  * manually releases any leg already held if the wait is abandoned
    (interrupt/timeout), so no partial hold leaks;
  * gives an UNLOAD-phase operator request higher priority than a fresh
    LOAD-phase request, so an unload re-request does not queue behind every
    new arrival (REQ-007).

Public API committed here (implementer conforms at T-005):

    ctx = RunContext(run_id: str, seed: int, horizon: float)
        ctx.env      -> simpy.Environment, clock starts at 0
        ctx.run_id   -> str, stored verbatim
        ctx.horizon  -> float, stored verbatim

    acquirer = ResourceAcquirer(machine_pool: simpy.PriorityResource,
                                 labor_pool: simpy.PriorityResource)

    # A SimPy generator: drive it with `yield from` (or `env.process(...)`).
    # On success it returns (machine_request, operator_request) — the same
    # request objects `simpy.PriorityResource.request()` returns, so the
    # caller releases them directly via `pool.release(request)`.
    # On abandon (simpy.Interrupt while waiting on the operator leg) it
    # releases the machine leg it already held, then re-raises the Interrupt.
    handles = yield from acquirer.acquire(priority: int)

Priority convention chosen here (matches simpy.PriorityResource: a SMALLER
int is served first):
    PRIORITY_UNLOAD = 0    # unload-phase operator re-request — served first
    PRIORITY_LOAD = 10     # fresh load-phase request — served after
"""

from __future__ import annotations

import simpy

from factory_twin.engine.acquire import ResourceAcquirer
from factory_twin.engine.clock import RunContext

PRIORITY_UNLOAD = 0
PRIORITY_LOAD = 10


def _make_context() -> RunContext:
    return RunContext(run_id="test-run", seed=1, horizon=1_000.0)


# ---------------------------------------------------------------------------
# COMP-001 RunContext
# ---------------------------------------------------------------------------


def test_run_context_exposes_a_live_simpy_environment_at_time_zero() -> None:
    ctx = _make_context()

    assert isinstance(ctx.env, simpy.Environment)
    assert ctx.env.now == 0


def test_run_context_stores_run_id_and_horizon() -> None:
    ctx = RunContext(run_id="rep-0042", seed=7, horizon=250.0)

    assert ctx.run_id == "rep-0042"
    assert ctx.horizon == 250.0


def test_run_context_instances_have_independent_environments() -> None:
    ctx_a = _make_context()
    ctx_b = _make_context()

    def advance(env: simpy.Environment):
        yield env.timeout(5)

    ctx_a.env.process(advance(ctx_a.env))
    ctx_a.env.run(until=10)

    assert ctx_a.env.now == 10
    assert ctx_b.env.now == 0


# ---------------------------------------------------------------------------
# COMP-002 ResourceAcquirer
# ---------------------------------------------------------------------------


def test_no_deadlock_stress_two_location_types_share_machine_and_labor_pools() -> None:
    """Many jobs across two distinct Location-like processes, both routed
    through ResourceAcquirer, contend for the SAME small machine and labor
    pools. The fixed machine-then-operator order lives inside
    ResourceAcquirer alone, so no caller can reintroduce the opposite-order
    deadlock. The run must terminate with every job completed — env.run()
    returning is not proof by itself, since a true deadlock also drains the
    event queue and returns silently with jobs stuck.
    """
    ctx = _make_context()
    machine_pool = simpy.PriorityResource(ctx.env, capacity=2)
    labor_pool = simpy.PriorityResource(ctx.env, capacity=2)
    acquirer = ResourceAcquirer(machine_pool, labor_pool)

    completed: list[str] = []

    def location(name: str, hold_time: float, index: int):
        yield ctx.env.timeout(index * 0.1)
        machine, operator = yield from acquirer.acquire(priority=PRIORITY_LOAD)
        try:
            yield ctx.env.timeout(hold_time)
        finally:
            labor_pool.release(operator)
            machine_pool.release(machine)
        completed.append(f"{name}-{index}")

    job_count = 20
    for i in range(job_count):
        ctx.env.process(location("type_a", 3.0, i))
        ctx.env.process(location("type_b", 2.0, i))

    ctx.env.run(until=500.0)

    assert len(completed) == job_count * 2


def test_abandoned_wait_releases_the_machine_leg_already_held() -> None:
    """A process acquires the machine leg (immediately available) then
    blocks on the operator leg (busy). It is interrupted while waiting.
    ResourceAcquirer must release the machine it already held so a later,
    unrelated request can get it — no partial hold leaks.
    """
    ctx = _make_context()
    machine_pool = simpy.PriorityResource(ctx.env, capacity=1)
    labor_pool = simpy.PriorityResource(ctx.env, capacity=1)
    acquirer = ResourceAcquirer(machine_pool, labor_pool)

    outcome: dict[str, bool] = {"interrupted": False}

    def operator_hog():
        with labor_pool.request(priority=PRIORITY_LOAD) as req:
            yield req
            yield ctx.env.timeout(1_000.0)

    def abandoning_location():
        try:
            yield from acquirer.acquire(priority=PRIORITY_LOAD)
        except simpy.Interrupt:
            outcome["interrupted"] = True

    def interrupter(target: simpy.Process):
        yield ctx.env.timeout(5.0)
        target.interrupt()

    ctx.env.process(operator_hog())
    abandoner = ctx.env.process(abandoning_location())
    ctx.env.process(interrupter(abandoner))
    ctx.env.run(until=20.0)

    assert outcome["interrupted"] is True

    # The machine must be free again: a fresh, unrelated request for it
    # succeeds within the run, which is only possible if the abandoned
    # holder's machine leg was released rather than leaked.
    freed: dict[str, bool] = {"acquired": False}

    def probe():
        with machine_pool.request(priority=PRIORITY_LOAD) as req:
            yield req
            freed["acquired"] = True

    ctx.env.process(probe())
    ctx.env.run(until=25.0)

    assert freed["acquired"] is True


def test_unload_priority_operator_request_outranks_queued_fresh_load_request() -> None:
    """An unload-phase operator re-request (PRIORITY_UNLOAD) must be served
    before a fresh load-phase request (PRIORITY_LOAD) that was already
    queued on the same labor pool, even though the load request arrived
    and queued first (D-045 / REQ-007) — an unload re-request must not
    queue behind every new arrival.
    """
    ctx = _make_context()
    machine_pool = simpy.PriorityResource(ctx.env, capacity=2)
    labor_pool = simpy.PriorityResource(ctx.env, capacity=1)
    acquirer = ResourceAcquirer(machine_pool, labor_pool)

    order: list[str] = []

    def operator_hog():
        with labor_pool.request(priority=PRIORITY_LOAD) as req:
            yield req
            yield ctx.env.timeout(10.0)

    def load_request():
        yield ctx.env.timeout(1.0)
        machine, operator = yield from acquirer.acquire(priority=PRIORITY_LOAD)
        order.append("load")
        labor_pool.release(operator)
        machine_pool.release(machine)

    def unload_request():
        yield ctx.env.timeout(2.0)
        machine, operator = yield from acquirer.acquire(priority=PRIORITY_UNLOAD)
        order.append("unload")
        labor_pool.release(operator)
        machine_pool.release(machine)

    ctx.env.process(operator_hog())
    ctx.env.process(load_request())
    ctx.env.process(unload_request())

    ctx.env.run(until=30.0)

    assert order == ["unload", "load"]
