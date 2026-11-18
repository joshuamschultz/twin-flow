"""Simulation performance budget on the scaled ERP shop (T-190, REQ-144, COMP-138, D-109).

Through the service path (`PlanningService.simulate_plan` / `simulate_portfolio`, which
re-verify the plan, read the signed `[simulation]` table and run `evaluate_candidates` on
the planning runner), on `fixtures.generate.scaled_shop(orders=600)` built by
`build_problem` (3,069 operations on 80 machines in 6 work centers; the PLAN's "about 4,800"
predates the generator) and solved by the TF-003 double (`FakePortfolioApi`, real baseline
solving). The profile source: lognormal run factor (mean 1.0, cv 0.15) and setup factor
(mean 1.1, cv 0.3) on every work center, breakdowns (MTBF 8 h, lognormal repair 30 min,
cv 0.5) on every fourth machine (20 of 80).

Budgets (REQ-144):

* `simulate_plan`, 10 replications: under 30 s with 1 worker, under 15 s with 4 workers.
* Peak resident memory under 1 GiB, read with `resource.getrusage` for this process
  (`RUSAGE_SELF`) and, separately, for the largest spawned worker (`RUSAGE_CHILDREN`).
* `simulate_portfolio` with 6 stub candidates (`MenuFake`, all verified), the baseline and
  a 120 s budget, asked for 100 replications (the policy cap): returns within 120 + 15 s,
  `completed` or `truncated`.

Measured on the dev laptop (Apple M5 Max, Python 3.12), 2026-10-11, alone (`pytest
tests/planning/test_simulation_performance.py`):

* 1 worker, 10 replications: 1.7 to 2.0 s (budget 30 s)
* 4 workers, 10 replications: 3.7 to 4.6 s (budget 15 s; slower than 1 worker at this size:
  spawning the pool and shipping the shop to each worker cost more than 10 replications)
* portfolio, 6 candidates + baseline, 100 replications, 120 s budget: 37.4 s, `completed`
  (budget 135 s)
* peak RSS: self 322 to 327 MiB, largest worker 153 to 154 MiB (budget 1,024 MiB each)
"""

from __future__ import annotations

import os
import resource
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_portfolio import FakePortfolioApi
from _simulation_world import CLASS_WEIGHTS, MenuFake

from twinflow.application.planning import PlanningService
from twinflow.application.workspace import Workspace
from twinflow.erp.profile import PlanningProfile, load_planning_profile
from twinflow.erp.schema import ErpSnapshot

pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[2]
CALLER = "perf"
REPLICATIONS = 10
ONE_WORKER_SECONDS = 30.0
FOUR_WORKER_SECONDS = 15.0
PORTFOLIO_BUDGET_SECONDS = 120.0
PORTFOLIO_GRACE_SECONDS = 15.0
PEAK_RSS_BYTES = 1024**3
STUB_CANDIDATES = 6
PORTFOLIO_REPLICATIONS = 100  # the policy's default replication cap: the most a caller may ask
SERVICE_SIMULATE_CAP = 300.0  # the policy maximum; the plan budgets are the asserted ones


def _scaled_shop() -> ErpSnapshot:
    erp_tests = str(ROOT / "tests" / "erp")
    if erp_tests not in sys.path:
        sys.path.insert(0, erp_tests)
    from fixtures.generate import scaled_shop

    return scaled_shop(orders=600)


def _simulation_toml(snapshot: ErpSnapshot, workers: int) -> str:
    """The profile source of REQ-144: factors on every work center, breakdowns on 1 in 4."""
    lines = [
        "[simulation]",
        'distribution_source = "profile"',
        f"replications = {REPLICATIONS}",
        f"workers = {workers}",
        "time_budget_seconds = 300",
        "max_candidates = 50",
        "",
    ]
    for work_center in sorted({m.work_center_id for m in snapshot.machines}):
        lines += [
            "[[simulation.run_factors]]",
            f'work_center_id = "{work_center}"',
            'distribution = { kind = "lognormal", mean = 1.0, cv = 0.15 }',
            "",
            "[[simulation.setup_factors]]",
            f'work_center_id = "{work_center}"',
            'distribution = { kind = "lognormal", mean = 1.1, cv = 0.3 }',
            "",
        ]
    for machine in sorted(m.id for m in snapshot.machines)[::4]:
        lines += [
            "[[simulation.breakdowns]]",
            f'machine_id = "{machine}"',
            "mtbf_seconds = 28800",
            'repair = { kind = "lognormal", mean = 1800.0, cv = 0.5 }',
            "",
        ]
    return "\n".join(lines)


def _profile(snapshot: ErpSnapshot, workers: int) -> PlanningProfile:
    return load_planning_profile(CLASS_WEIGHTS + "\n" + _simulation_toml(snapshot, workers))


_CHILD_FLAG = "TWINFLOW_PERF_CHILD"


def _peak_rss_bytes(who: int) -> int:
    """`ru_maxrss` in bytes (macOS reports bytes, Linux kilobytes)."""
    peak = resource.getrusage(who).ru_maxrss
    return int(peak if sys.platform == "darwin" else peak * 1024)


@pytest.fixture(scope="module")
def snapshot() -> ErpSnapshot:
    return _scaled_shop()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    ws = Workspace(tmp_path / "workspace")
    yield ws
    ws.close()


def _service(workspace: Workspace, portfolio: FakePortfolioApi) -> PlanningService:
    return PlanningService(
        workspace,
        portfolio=portfolio,
        max_simulate_seconds=SERVICE_SIMULATE_CAP,
        max_simulation_workers=4,
    )


def _solved(
    service: PlanningService, snapshot: ErpSnapshot, workers: int, key: str
) -> tuple[str, str]:
    """(portfolio id, recommended plan id) of the scaled shop under the REQ-144 profile."""
    built = service.build_problem(snapshot, _profile(snapshot, workers))
    assert built.status == "ready", [i.code for i in built.issues]
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(
        built.problem_id, request_key=key, caller_id=CALLER, time_budget_seconds=60.0
    )
    assert portfolio.recommended_plan_id is not None, portfolio
    return portfolio.portfolio_id, str(portfolio.recommended_plan_id)


@pytest.mark.parametrize(
    ("workers", "limit_seconds"), [(1, ONE_WORKER_SECONDS), (4, FOUR_WORKER_SECONDS)]
)
def test_simulate_plan_on_the_scaled_shop_meets_the_runtime_and_memory_budget(
    request: pytest.FixtureRequest,
    workspace: Workspace,
    snapshot: ErpSnapshot,
    workers: int,
    limit_seconds: float,
) -> None:
    # `ru_maxrss` is the lifetime peak of the whole process. In a full pytest run, earlier
    # tests leave that peak behind, so the budget is measured in a fresh child process
    # that runs only this test (TF-006: the full suite grew past the 1 GiB line).
    if os.environ.get(_CHILD_FLAG) != "1":
        child = subprocess.run(  # noqa: S603 - fixed interpreter and this test's own node id
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                request.node.nodeid,
            ],
            cwd=request.config.rootpath,
            env={**os.environ, _CHILD_FLAG: "1"},
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
        print(child.stdout[-2000:])
        assert child.returncode == 0, child.stdout[-4000:] + child.stderr[-2000:]
        return
    service = _service(workspace, FakePortfolioApi())
    try:
        _, plan_id = _solved(service, snapshot, workers, f"solve-{workers}")

        started = time.perf_counter()
        result = service.simulate_plan(plan_id, request_key=f"sim-{workers}", caller_id=CALLER)
        elapsed = time.perf_counter() - started
    finally:
        service.close()

    peak_self = _peak_rss_bytes(resource.RUSAGE_SELF)
    peak_child = _peak_rss_bytes(resource.RUSAGE_CHILDREN)
    print(
        f"simulate_plan {workers} worker(s), {REPLICATIONS} reps: {elapsed:.1f} s; "
        f"peak RSS self {peak_self / 2**20:.0f} MiB, largest child {peak_child / 2**20:.0f} MiB"
    )
    assert result.status == "completed", result.issues
    assert result.summary is not None
    assert result.summary.replications == REPLICATIONS
    assert result.summary.deterministic is False
    metrics = result.summary.metrics
    assert metrics["censored_jobs"].max == 0, "every replication must finish every job"
    assert metrics["makespan"].std > 0, "the profile source must vary the replications"
    assert elapsed < limit_seconds, f"{elapsed:.1f} s with {workers} worker(s)"
    assert peak_self < PEAK_RSS_BYTES, f"self {peak_self / 2**20:.0f} MiB"
    assert peak_child < PEAK_RSS_BYTES, f"child {peak_child / 2**20:.0f} MiB"


def test_simulate_portfolio_of_six_candidates_and_the_baseline_returns_within_budget(
    workspace: Workspace, snapshot: ErpSnapshot
) -> None:
    ids = tuple(f"stub-{index}" for index in range(STUB_CANDIDATES))
    # Unshifted copies of the verified baseline plan: every stub verifies on the calendars.
    fake = MenuFake(tuple((cid, 0, True) for cid in ids), menu=ids, recommended=ids[0])
    service = _service(workspace, fake)
    try:
        portfolio_id, _ = _solved(service, snapshot, 4, "solve-portfolio")

        started = time.perf_counter()
        result = service.simulate_portfolio(
            portfolio_id,
            request_key="sim-portfolio",
            caller_id=CALLER,
            reps=PORTFOLIO_REPLICATIONS,
            time_budget_seconds=PORTFOLIO_BUDGET_SECONDS,
        )
        elapsed = time.perf_counter() - started
    finally:
        service.close()

    print(
        f"simulate_portfolio {len(result.candidates)} candidates + baseline: {elapsed:.1f} s, "
        f"{result.status}, {result.replications} replication(s)"
    )
    assert result.status in ("completed", "truncated"), result.issues
    assert len(result.candidates) == STUB_CANDIDATES, result.issues
    assert result.baseline is not None
    assert result.replications >= 1
    assert elapsed < PORTFOLIO_BUDGET_SECONDS + PORTFOLIO_GRACE_SECONDS, f"{elapsed:.1f} s"
