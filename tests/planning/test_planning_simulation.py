"""Planning service simulation: `simulate_plan`, `simulate_portfolio`, `compare_plans`, evidence.

Task T-187 (COMP-139). Requirements: REQ-127, REQ-136, REQ-138, REQ-139, REQ-140, REQ-141,
REQ-143. Decisions: D-087 (re-verify before simulating), D-104 (policy = `[simulation]` of
the signed profile; `profile_digest` re-judges under another signed profile), D-105
(synchronous, capped, idempotent, stored by reference), D-108 (portfolio candidate order and
cap). Supersedes TF-005 REQ-171 (`simulation_link_pending`, `wait_seconds`, the 10-rep cap).

Contract pinned here (the implementation follows these tests):

* `PlanningService(workspace, *, portfolio, max_solve_seconds=120.0,
  timeout_grace_seconds=15.0, max_workers=4, max_simulate_seconds=120.0,
  max_simulation_replications=200, max_simulation_candidates=20, max_simulation_workers=4)`.
  The service layer passes `ServiceSettings` values in (as it does `max_solve_seconds`);
  the application layer never imports `twinflow.service`.
* `simulate_plan(plan_id, *, request_key, caller_id, reps=None, seed=42,
  time_budget_seconds=None, baseline=False, profile_digest=None, artifacts=None)
  -> SimulateResult{plan_id, status: "completed" | "truncated" | "blocked", simulation_id,
  issues, summary: CandidateSummary | None, baseline: CandidateSummary | None, validity,
  evidence_kind: "simulation"}` (no `job` field). The plan id is an ERP `plans` id or a
  scenario `schedule_plans` id. Before simulating, `plan_source.check_plan` re-verifies the
  plan; blocked gives status `blocked` with its issues (`plan_unverified` or
  `schedule_source_changed` first), `simulation_id` None, and stores nothing.
* `simulate_portfolio(portfolio_id, *, request_key, caller_id, reps=None, seed=42,
  time_budget_seconds=None, baseline=True, profile_digest=None, artifacts=None)
  -> PortfolioSimulation{evaluation_id, portfolio_id, status, replications, seed,
  candidates: tuple[CandidateSummary] (in simulation order), baseline, ranking, issues,
  validity, evidence_kind: "portfolio_simulation"}`. Order (D-108): recommended, then menu
  entries in menu order, then the rest by candidate id; candidates whose stored `verified`
  is false or that fail re-verification are skipped with a `candidate_unverified` issue;
  verified candidates past `min(policy.max_candidates, max_simulation_candidates)` get a
  `candidate_cap` issue. Each such issue names the plan id in its `path` or `message`.
  All simulated candidates go to ONE `evaluate_candidates` call in that order.
* Caps (REQ-139, REQ-143): replications = `reps` when given, else `policy.replications`
  clamped to the caps; `reps` above `min(policy.max_replications,
  max_simulation_replications)` raises `SizeLimit` and stores nothing. Budget =
  `min(time_budget_seconds or inf, policy.time_budget_seconds, max_simulate_seconds)`
  (clamped, never refused). Workers = `min(policy.workers, max_simulation_workers)`.
  Baseline rule = `policy.baseline_rule`. Bad `reps` (< 1), seed or budget -> `ValueError`.
* Policy (D-104): the `[simulation]` table of the problem's recorded profile, re-verified
  through `artifacts.for_solve` like a solve (broken signature -> `ArtifactSignatureInvalid`,
  nothing stored); `profile_digest` names another stored signed profile (unknown ->
  `NotFound`). Scenario plans use `CapsuleSource(scenario capsule model)` and
  `SimulationPolicy()`.
* Engine: the service imports `twinflow.execution.evaluate` lazily, by its full module name,
  at call time and calls its module attribute `evaluate_candidates` (so a spy installed with
  `monkeypatch.setattr` sees every call); when that import fails the simulate methods raise
  `CapabilityUnavailable` and store nothing.
* Idempotency (TF-005 D-120): the same caller, key and inputs replay the stored result
  without simulating; other inputs give `IdempotencyConflict`; another caller never matches.
* Records (SDD Data Model "Records"): `simulations/sim-{uuid32}` per plan and per baseline,
  `evaluations/ev-{uuid32}` per portfolio call.
* `compare_plans(plan_ids, simulation_ids=None)`: `simulation` is None without ids; with one
  stored simulation per plan (same order) it is `{seed, replications, reference_plan_id,
  deltas: {plan_id: {metric: PairedDelta}}, ranking}`; a simulation of another plan, or
  another seed, policy or source, raises `PlanningError` code `incomparable_simulations`.
* `simulation_evidence(simulation_id, *, entity_id=None, offset=0, limit=100)` -> page with
  `simulation_id, total, offset, limit, next_offset, rows` (the record's operation rows,
  then its job rows, optionally filtered to rows whose plan, job, operation or machine id
  equals `entity_id`); `limit` outside 1..1000 or `offset` < 0 -> `ValueError`.
"""

from __future__ import annotations

import math
import re
import statistics
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from _fake_portfolio import FakePortfolioApi, tiny_profile, tiny_shop
from _simulation_world import (
    SEED_CALIBRATION,
    MenuFake,
    erp_profile,
    real_portfolio_api,
    scenario_plan,
    small_shop,
)
from pydantic import BaseModel

import twinflow.execution as execution_facade
import twinflow.execution.evaluate as evaluate_module
from twinflow.application.planning import (
    CapabilityUnavailable,
    IdempotencyConflict,
    NotFound,
    PlanningError,
    PlanningService,
    SizeLimit,
)
from twinflow.application.workspace import Workspace
from twinflow.erp.profile import PlanningProfile
from twinflow.execution import CapsuleSource, DeterministicSource, SimulationPolicy, policy_digest
from twinflow.execution.evaluate import paired_delta
from twinflow.execution.metrics import METRIC_NAMES
from twinflow.execution.sources import ProfileSource
from twinflow.scheduling import (
    ObjectiveKind,
    Schedule,
    SolveRequest,
    apply_overrides,
    rank,
    verify,
)

ROOT = Path(__file__).resolve().parents[2]
CALLER = "arc"
SIM_KINDS = ("simulations", "evaluations")
STATS_FIELDS = {"mean", "std", "min", "p10", "p50", "p90", "max", "ci_low", "ci_high", "n"}
DELTA_FIELDS = {"mean_difference", "low", "high", "n"}
SIM_ID = re.compile(r"^sim-[0-9a-f]{32}$")
EV_ID = re.compile(r"^ev-[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


# --- helpers -----------------------------------------------------------------------------------


def _row(value: Any) -> dict[str, Any]:
    """A result or nested row as JSON, whether the service returns a model or a dict."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    assert isinstance(value, dict), type(value)
    return value


def _counts(workspace: Workspace) -> dict[str, int]:
    return {kind: workspace.repository.count(kind) for kind in SIM_KINDS}


def _simulations(workspace: Workspace) -> list[dict[str, Any]]:
    return workspace.repository.list("simulations", limit=1000)


def _codes(result: Any) -> list[str]:
    return [issue["code"] for issue in _row(result)["issues"]]


def _issues_naming(result: Any, code: str) -> list[str]:
    """The `path` + `message` text of every issue with `code`."""
    return [
        f"{issue['path']} {issue['message']}"
        for issue in _row(result)["issues"]
        if issue["code"] == code
    ]


def _per_rep(record: dict[str, Any], metric: str) -> dict[int, float]:
    return {int(rep["rep"]): float(rep["metrics"][metric]) for rep in record["per_rep"]}


def _mean_objectives(record: dict[str, Any]) -> dict[ObjectiveKind, float]:
    kinds = record["per_rep"][0]["objective_values"]
    return {
        ObjectiveKind(kind): statistics.fmean(
            float(rep["objective_values"][kind]) for rep in record["per_rep"]
        )
        for kind in kinds
    }


def _stochastic(profile: PlanningProfile) -> PlanningProfile:
    """`profile` with the seed calibration `[simulation]` table (profile source)."""
    return profile.model_copy(update={"simulation": erp_profile(SEED_CALIBRATION).simulation})


def _with_simulation(profile: PlanningProfile, **changes: Any) -> PlanningProfile:
    policy = SimulationPolicy.model_validate(
        {**profile.simulation.model_dump(mode="json"), **changes}
    )
    return profile.model_copy(update={"simulation": policy})


def _really_verifies(workspace: Workspace, plan_id: str) -> bool:
    record = workspace.repository.get("plans", plan_id)
    request = SolveRequest.model_validate(
        workspace.repository.get("problems", record["problem_id"])["request"]
    )
    assignments = [{k: v for k, v in a.items() if k != "job_id"} for a in record["assignments"]]
    schedule = Schedule.model_validate({**record["schedule"], "assignments": assignments})
    compiled = apply_overrides(request)
    stripped = schedule.model_copy(update={"primary_value": None})
    return verify(compiled, request.overrides, stripped, request.previous_plan).valid


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    ws = Workspace(tmp_path / "workspace")  # the signing key must live outside it
    yield ws
    ws.close()


@pytest.fixture(params=["fake", "real"])
def api(request: pytest.FixtureRequest) -> Any:
    if request.param == "fake":
        return FakePortfolioApi()
    return real_portfolio_api()


@pytest.fixture
def make_service(workspace: Workspace) -> Iterator[Callable[..., PlanningService]]:
    """Build services over the shared workspace; every one is closed at teardown."""
    made: list[PlanningService] = []

    def make(portfolio: Any = None, **limits: Any) -> PlanningService:
        service = PlanningService(
            workspace,
            portfolio=portfolio if portfolio is not None else FakePortfolioApi(),
            **limits,
        )
        made.append(service)
        return service

    yield make
    for service in made:
        service.close()


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every `evaluate_candidates` call (plan ids in order plus keyword args)."""
    calls: list[dict[str, Any]] = []
    original = evaluate_module.evaluate_candidates

    def recording(*args: Any, **kwargs: Any) -> Any:
        candidates = args[0] if args else kwargs["candidates"]
        calls.append({"plan_ids": [c.plan_id for c in candidates], **kwargs})
        return original(*args, **kwargs)

    monkeypatch.setattr(evaluate_module, "evaluate_candidates", recording)
    monkeypatch.setattr(execution_facade, "evaluate_candidates", recording)
    return calls


def _erp_plan(service: PlanningService, profile: PlanningProfile, key: str = "solve") -> str:
    """The recommended verified plan of the small shop under `profile`."""
    built = service.build_problem(small_shop(), profile)
    assert built.status == "ready", built.issues
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(
        built.problem_id, request_key=key, caller_id=CALLER, time_budget_seconds=10.0
    )
    assert portfolio.recommended_plan_id is not None
    return str(portfolio.recommended_plan_id)


def _simulate(service: PlanningService, plan_id: str, key: str = "sim", **options: Any) -> Any:
    return service.simulate_plan(plan_id, request_key=key, caller_id=CALLER, **options)


# --- simulate_plan on an ERP plan -----------------------------------------------------------------


def test_simulate_plan_erp_plan_completes_and_stores_one_simulation(
    workspace: Workspace, make_service: Callable[..., PlanningService], api: Any
) -> None:
    service = make_service(api)
    plan_id = _erp_plan(service, erp_profile())

    result = _row(_simulate(service, plan_id))

    assert result["status"] == "completed", result["issues"]
    assert result["plan_id"] == plan_id
    assert SIM_ID.match(result["simulation_id"])
    assert result["validity"] == "provisional"
    assert result["evidence_kind"] == "simulation"
    assert "job" not in result
    assert result["baseline"] is None
    assert workspace.repository.count("simulations") == 1
    assert workspace.repository.count("evaluations") == 0


def test_simulate_plan_record_has_every_data_model_field(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service()
    profile = erp_profile()
    plan_id = _erp_plan(service, profile)
    plan = workspace.repository.get("plans", plan_id)

    result = _row(_simulate(service, plan_id))
    record = workspace.repository.get("simulations", result["simulation_id"])

    assert record["id"] == result["simulation_id"]
    assert record["plan_id"] == plan_id
    assert record["kind"] == "plan"
    assert record["source_kind"] == "erp"
    assert record["problem_id"] == plan["problem_id"]
    assert record["problem_digest"] == plan["problem_digest"]
    assert SHA256.match(record["plan_digest"])
    assert record["seed"] == 42
    assert record["replications"] == 1
    assert record["deterministic"] is True
    assert record["status"] == "completed"
    assert record["policy_digest"] == policy_digest(profile.simulation)
    assert record["source"] == {"name": "deterministic", "digest": DeterministicSource().digest}
    assert record["budget_seconds"] == 60.0  # SimulationPolicy default under the 120 s cap
    assert record["elapsed_seconds"] >= 0.0
    assert record["summary"] == result["summary"]
    assert len(record["per_rep"]) == 1
    assert set(record["per_rep"][0]["metrics"]) == set(METRIC_NAMES)
    assert record["per_rep"][0]["objective_values"]
    assert {row["row"] for row in record["operations"]} == {"operation"}
    assert {row["row"] for row in record["jobs"]} == {"job"}
    assert {row["operation_id"] for row in record["operations"]} == {
        a["operation_id"] for a in plan["assignments"]
    }
    assert record["created_at"]
    assert record["validity"] == "provisional"
    assert record["evidence_kind"] == "simulation"


def test_simulate_plan_summary_is_a_candidate_summary_of_the_deterministic_replay(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())
    planned = workspace.repository.get("plans", plan_id)["objective_values"]

    summary = _row(_simulate(service, plan_id))["summary"]

    assert summary["plan_id"] == plan_id
    assert summary["kind"] == "plan"
    assert summary["replications"] == 1
    assert summary["deterministic"] is True
    assert summary["status"] == "completed"
    assert summary["seed"] == 42
    assert set(summary["metrics"]) == set(METRIC_NAMES)
    for stats in summary["metrics"].values():
        assert set(stats) == STATS_FIELDS
        assert stats["n"] == 1
    # A deterministic replay realizes the plan exactly (REQ-131), so the simulated weighted
    # tardiness is the plan's own value.
    assert math.isclose(
        summary["metrics"]["weighted_tardiness"]["mean"],
        planned["weighted_tardiness"],
        abs_tol=1e-6,
    )
    assert math.isclose(summary["planned"]["weighted_tardiness"], planned["weighted_tardiness"])
    assert set(summary["bottleneck"]) == {"machine_id", "planned_utilization"}
    assert set(summary["robustness"]) >= {"planned_r", "delta_curve", "load_check"}
    assert summary.get("baseline_delta") in (None, {})


def test_simulate_plan_seed_calibration_profile_runs_the_requested_replications(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service()
    profile = erp_profile(SEED_CALIBRATION)
    plan_id = _erp_plan(service, profile)

    result = _row(_simulate(service, plan_id, reps=4, seed=7))
    record = workspace.repository.get("simulations", result["simulation_id"])

    assert result["status"] == "completed"
    assert result["summary"]["deterministic"] is False
    assert result["summary"]["replications"] == 4
    assert result["summary"]["metrics"]["makespan"]["n"] == 4
    assert record["replications"] == 4
    assert [rep["rep"] for rep in record["per_rep"]] == [0, 1, 2, 3]
    assert record["seed"] == 7
    assert record["source"] == {
        "name": "profile",
        "digest": ProfileSource(profile.simulation).digest,
    }
    assert len(set(_per_rep(record, "makespan").values())) > 1, "replications should differ"


def test_simulate_plan_deterministic_default_collapses_to_one_replication(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    result = _row(_simulate(service, plan_id, reps=10))

    assert result["summary"]["deterministic"] is True
    assert result["summary"]["replications"] == 1
    record = workspace.repository.get("simulations", result["simulation_id"])
    assert record["replications"] == 1


def test_simulate_plan_same_seed_is_reproducible_and_plan_digest_stable(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile(SEED_CALIBRATION))

    first = _row(_simulate(service, plan_id, key="a", reps=3, seed=5))
    second = _row(_simulate(service, plan_id, key="b", reps=3, seed=5))

    one = workspace.repository.get("simulations", first["simulation_id"])
    two = workspace.repository.get("simulations", second["simulation_id"])
    assert first["simulation_id"] != second["simulation_id"]
    assert one["per_rep"] == two["per_rep"]
    assert one["plan_digest"] == two["plan_digest"]


def test_simulate_plan_with_baseline_adds_the_dispatch_baseline_and_paired_deltas(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    spy: list[dict[str, Any]],
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile(SEED_CALIBRATION))

    result = _row(_simulate(service, plan_id, reps=3, baseline=True))

    assert result["status"] == "completed"
    assert spy[-1]["baseline_rule"] == "edd"
    baseline = result["baseline"]
    assert baseline["kind"] == "baseline"
    assert baseline["plan_id"] is None
    assert SIM_ID.match(baseline["simulation_id"])
    delta = result["summary"]["baseline_delta"]
    assert set(delta) == set(METRIC_NAMES)
    for paired in delta.values():
        assert set(paired) == DELTA_FIELDS
        assert paired["n"] == 3
    records = {r["kind"]: r for r in _simulations(workspace)}
    assert set(records) == {"plan", "baseline"}
    assert records["baseline"]["plan_id"] is None
    assert records["baseline"]["baseline_rule"] == "edd"
    assert records["baseline"]["id"] == baseline["simulation_id"]
    # The paired delta is candidate minus baseline over common replications (D-103).
    expected = paired_delta(
        _per_rep(records["plan"], "weighted_tardiness"),
        _per_rep(records["baseline"], "weighted_tardiness"),
        erp_profile(SEED_CALIBRATION).simulation.confidence_level,
    )
    assert math.isclose(delta["weighted_tardiness"]["mean_difference"], expected.mean_difference)


def test_simulate_plan_baseline_rule_comes_from_the_policy(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, _with_simulation(erp_profile(), baseline_rule="spt"))

    _simulate(service, plan_id, baseline=True)

    assert spy[-1]["baseline_rule"] == "spt"


def test_simulate_plan_without_baseline_passes_no_baseline_rule(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    _simulate(service, plan_id)

    assert spy[-1]["baseline_rule"] is None
    assert spy[-1]["plan_ids"] == [plan_id]


# --- caps (REQ-139, REQ-143) ----------------------------------------------------------------------


def test_simulate_plan_reps_default_is_the_policy_value_clamped_to_the_service_cap(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service(max_simulation_replications=3)
    plan_id = _erp_plan(service, erp_profile(SEED_CALIBRATION))  # policy replications = 20

    result = _row(_simulate(service, plan_id))

    assert spy[-1]["replications"] == 3
    assert result["summary"]["replications"] == 3


def test_simulate_plan_reps_default_is_the_policy_replications_under_the_caps(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, _with_simulation(erp_profile(SEED_CALIBRATION), replications=6))

    _simulate(service, plan_id)

    assert spy[-1]["replications"] == 6


@pytest.mark.parametrize(
    ("service_cap", "policy_cap", "reps"),
    [(3, 100, 4), (200, 5, 6), (200, 100, 101), (200, 100, 1000)],
)
def test_simulate_plan_reps_over_either_cap_is_size_limit_and_stores_nothing(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    spy: list[dict[str, Any]],
    service_cap: int,
    policy_cap: int,
    reps: int,
) -> None:
    service = make_service(max_simulation_replications=service_cap)
    profile = _with_simulation(
        erp_profile(SEED_CALIBRATION), replications=min(5, policy_cap), max_replications=policy_cap
    )
    plan_id = _erp_plan(service, profile)

    with pytest.raises(SizeLimit):
        _simulate(service, plan_id, reps=reps)

    assert spy == []
    assert _counts(workspace) == {"simulations": 0, "evaluations": 0}


def test_simulate_plan_reps_at_the_cap_is_accepted(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service(max_simulation_replications=3)
    plan_id = _erp_plan(service, erp_profile(SEED_CALIBRATION))

    _simulate(service, plan_id, reps=3)

    assert spy[-1]["replications"] == 3


@pytest.mark.parametrize(
    ("requested", "policy_budget", "service_cap", "expected"),
    [
        (None, 60.0, 120.0, 60.0),
        (30.0, 60.0, 120.0, 30.0),
        (100.0, 60.0, 45.0, 45.0),
        (None, 200.0, 120.0, 120.0),
        (500.0, 300.0, 120.0, 120.0),
    ],
)
def test_simulate_plan_budget_is_the_min_of_request_policy_and_service_cap(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    spy: list[dict[str, Any]],
    requested: float | None,
    policy_budget: float,
    service_cap: float,
    expected: float,
) -> None:
    service = make_service(max_simulate_seconds=service_cap)
    plan_id = _erp_plan(service, _with_simulation(erp_profile(), time_budget_seconds=policy_budget))

    result = _row(_simulate(service, plan_id, time_budget_seconds=requested))

    assert spy[-1]["budget_seconds"] == expected
    record = workspace.repository.get("simulations", result["simulation_id"])
    assert record["budget_seconds"] == expected


def test_simulate_plan_workers_are_the_min_of_policy_and_service_cap(
    make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service(max_simulation_workers=2)
    plan_id = _erp_plan(service, _with_simulation(erp_profile(), workers=3))

    _simulate(service, plan_id)

    assert spy[-1]["workers"] == 2


def test_simulate_plan_passes_the_profile_policy_and_load_targets(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    profile = erp_profile(SEED_CALIBRATION)
    plan_id = _erp_plan(service, profile)

    _simulate(service, plan_id, reps=2, seed=9)

    call = spy[-1]
    assert call["policy"] == profile.simulation
    assert call["seed"] == 9
    assert tuple(call["targets"]) == tuple(profile.scheduling.robustness_load_targets)
    assert call["source"].digest == ProfileSource(profile.simulation).digest


@pytest.mark.parametrize(
    "options",
    [
        {"reps": 0},
        {"reps": -1},
        {"seed": -1},
        {"seed": 2**32},
        {"time_budget_seconds": 0.0},
        {"time_budget_seconds": -5.0},
        {"time_budget_seconds": math.inf},
        {"time_budget_seconds": math.nan},
    ],
)
def test_simulate_plan_rejects_bad_settings_and_stores_nothing(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    options: dict[str, Any],
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    with pytest.raises(ValueError):
        _simulate(service, plan_id, **options)

    assert workspace.repository.count("simulations") == 0


def test_simulate_plan_requires_request_key(make_service: Callable[..., PlanningService]) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    with pytest.raises(ValueError):
        service.simulate_plan(plan_id, request_key="", caller_id=CALLER)


def test_simulate_plan_unknown_plan_is_not_found(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    with pytest.raises(NotFound):
        _simulate(make_service(), "nope")

    assert workspace.repository.count("simulations") == 0


# --- idempotency ---------------------------------------------------------------------------------


def test_simulate_plan_same_key_and_inputs_replays_without_simulating(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    first = _simulate(service, plan_id, reps=2, baseline=True)
    again = _simulate(service, plan_id, reps=2, baseline=True)

    assert again == first
    assert len(spy) == 1
    assert workspace.repository.count("simulations") == 2  # plan + baseline, once


@pytest.mark.parametrize(
    "changed",
    [{"reps": 3}, {"seed": 43}, {"baseline": True}, {"time_budget_seconds": 5.0}],
)
def test_simulate_plan_same_key_other_inputs_is_idempotency_conflict(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    spy: list[dict[str, Any]],
    changed: dict[str, Any],
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())
    _simulate(service, plan_id, reps=2)

    with pytest.raises(IdempotencyConflict):
        _simulate(service, plan_id, **{"reps": 2, **changed})

    assert len(spy) == 1
    assert workspace.repository.count("simulations") == 1


def test_simulate_plan_another_caller_with_the_same_key_never_matches(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())

    mine = _row(service.simulate_plan(plan_id, request_key="k", caller_id="alice"))
    theirs = _row(service.simulate_plan(plan_id, request_key="k", caller_id="bob"))

    assert mine["simulation_id"] != theirs["simulation_id"]
    assert len(spy) == 2
    assert workspace.repository.count("simulations") == 2


def test_simulate_plan_replay_survives_a_new_service_on_the_same_workspace(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    plan_id = _erp_plan(make_service(), erp_profile())
    first = _simulate(make_service(), plan_id)

    again = _simulate(make_service(), plan_id)

    assert again == first
    assert len(spy) == 1


# --- tampered plans are never simulated (D-087) ---------------------------------------------------


def test_simulate_plan_tampered_record_is_blocked_plan_unverified_and_stores_nothing(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())
    record = workspace.repository.get("plans", plan_id)
    del record["assignments"][0]
    workspace.repository.update("plans", plan_id, record)

    result = _row(_simulate(service, plan_id, baseline=True))

    assert result["status"] == "blocked"
    assert _codes(result)[0] == "plan_unverified"
    assert len(_codes(result)) >= 2, "the verifier issues follow plan_unverified"
    assert result["simulation_id"] is None
    assert result["summary"] is None
    assert result["validity"] == "provisional"
    assert spy == []
    assert _counts(workspace) == {"simulations": 0, "evaluations": 0}


def test_simulate_plan_record_flagged_verified_but_infeasible_is_blocked(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    """The stored `verified` flag is never trusted: an edited start must not simulate."""
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())
    record = workspace.repository.get("plans", plan_id)
    assert record["verified"] is True
    by_machine: dict[str, list[dict[str, Any]]] = {}
    for assignment in record["assignments"]:
        by_machine.setdefault(assignment["machine_id"], []).append(assignment)
    first, second = sorted(max(by_machine.values(), key=len), key=lambda a: a["start"])[:2]
    shift = first["start"] - second["start"]
    for field in ("start", "setup_end", "end"):
        second[field] += shift
    workspace.repository.update("plans", plan_id, record)

    result = _row(_simulate(service, plan_id))

    assert result["status"] == "blocked"
    assert _codes(result)[0] == "plan_unverified"
    assert spy == []
    assert workspace.repository.count("simulations") == 0


def test_no_simulation_link_pending_left_in_src() -> None:
    """REQ-140: the TF-005 placeholder is removed everywhere, not just bypassed."""
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src").rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".json", ".md", ".toml", ".yaml", ".yml", ".ts", ".tsx", ".js"}
        and "simulation_link_pending" in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert offenders == []


# --- signed profiles (D-104) ----------------------------------------------------------------------


@pytest.fixture
def signed(workspace: Workspace, tmp_path: Path) -> Any:
    """A real `SignedArtifactStore` over the workspace and its `PlanningArtifacts`."""
    pytest.importorskip("cryptography")
    from datetime import UTC, datetime

    from twinflow.application.artifacts import SignedArtifactStore
    from twinflow.application.planning_artifacts import PlanningArtifacts
    from twinflow.service.signing import ServiceSigner, generate_signing_key

    key_dir = tmp_path / "keys"
    key_dir.mkdir(mode=0o700)
    key_path = key_dir / "service-signing-key.pem"
    generate_signing_key(key_path)
    signer = ServiceSigner.load(key_path, workspace_root=workspace.repository.root)
    store = SignedArtifactStore(workspace.repository, signer, clock=lambda: datetime.now(UTC))
    return store, PlanningArtifacts(store, local_mode=False)


def _signed_plan(
    service: PlanningService, signed: Any, profile: PlanningProfile
) -> tuple[str, str]:
    """Put `profile`, build and solve under it; (plan id, profile digest)."""
    store, artifacts = signed
    ref = store.put("planning_profiles", profile, caller_id=CALLER)
    used, provenance = artifacts.for_build(None)
    assert used == profile
    built = service.build_problem(small_shop(), used, provenance=provenance)
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(
        built.problem_id, request_key="signed-solve", caller_id=CALLER, artifacts=artifacts
    )
    assert portfolio.recommended_plan_id is not None
    return str(portfolio.recommended_plan_id), str(ref.digest)


def _break_signature(workspace: Workspace, sequence_id: str) -> None:
    stored = workspace.repository.get("planning_profiles", sequence_id)
    tampered = stored["payload"].replace('"A-KEY":4', '"A-KEY":5')
    assert tampered != stored["payload"], stored["payload"]
    workspace.repository.update("planning_profiles", sequence_id, {**stored, "payload": tampered})


def test_simulate_plan_uses_the_problems_signed_profile_policy(
    workspace: Workspace, make_service: Callable[..., PlanningService], signed: Any
) -> None:
    service = make_service()
    profile = erp_profile()
    plan_id, _ = _signed_plan(service, signed, profile)

    result = _row(_simulate(service, plan_id, artifacts=signed[1]))

    assert result["status"] == "completed"
    record = workspace.repository.get("simulations", result["simulation_id"])
    assert record["policy_digest"] == policy_digest(profile.simulation)


def test_simulate_plan_broken_profile_signature_fails_closed_and_stores_nothing(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    signed: Any,
    spy: list[dict[str, Any]],
) -> None:
    service = make_service()
    plan_id, _ = _signed_plan(service, signed, erp_profile())
    _break_signature(workspace, "00000001")

    with pytest.raises(PlanningError) as raised:
        _simulate(service, plan_id, artifacts=signed[1])

    assert raised.value.code == "artifact_signature_invalid"
    assert spy == []
    assert _counts(workspace) == {"simulations": 0, "evaluations": 0}


def test_simulate_plan_profile_digest_rejudges_under_another_signed_profile(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    signed: Any,
    spy: list[dict[str, Any]],
) -> None:
    service = make_service()
    store, artifacts = signed
    plan_id, _ = _signed_plan(service, signed, erp_profile())
    calibrated = erp_profile(SEED_CALIBRATION)
    newer = store.put("planning_profiles", calibrated, caller_id=CALLER)

    result = _row(
        _simulate(service, plan_id, reps=3, profile_digest=newer.digest, artifacts=artifacts)
    )

    record = workspace.repository.get("simulations", result["simulation_id"])
    assert record["policy_digest"] == policy_digest(calibrated.simulation)
    assert record["source"]["name"] == "profile"
    assert record["deterministic"] is False
    assert record["replications"] == 3
    assert spy[-1]["policy"] == calibrated.simulation


def test_simulate_plan_unknown_profile_digest_is_not_found(
    workspace: Workspace, make_service: Callable[..., PlanningService], signed: Any
) -> None:
    service = make_service()
    plan_id, _ = _signed_plan(service, signed, erp_profile())

    with pytest.raises(NotFound):
        _simulate(service, plan_id, profile_digest="0" * 64, artifacts=signed[1])

    assert workspace.repository.count("simulations") == 0


def test_simulate_plan_named_profile_with_a_broken_signature_fails_closed(
    workspace: Workspace, make_service: Callable[..., PlanningService], signed: Any
) -> None:
    service = make_service()
    store, artifacts = signed
    plan_id, _ = _signed_plan(service, signed, erp_profile())
    newer = store.put("planning_profiles", erp_profile(SEED_CALIBRATION), caller_id=CALLER)
    _break_signature(workspace, "00000002")

    with pytest.raises(PlanningError) as raised:
        _simulate(service, plan_id, profile_digest=newer.digest, artifacts=artifacts)

    assert raised.value.code == "artifact_signature_invalid"
    assert workspace.repository.count("simulations") == 0


# --- scenario plans -------------------------------------------------------------------------------


def test_simulate_plan_scenario_plan_runs_through_the_executor_with_the_capsule_source(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = scenario_plan(workspace)
    plan = workspace.repository.get("schedule_plans", plan_id)
    scenario = workspace.repository.get("scenarios", plan["scenario_id"])

    result = _row(_simulate(service, plan_id))

    assert result["status"] == "completed", result["issues"]
    assert result["plan_id"] == plan_id
    record = workspace.repository.get("simulations", result["simulation_id"])
    assert record["source_kind"] == "scenario"
    assert record["problem_digest"] == plan["problem_digest"]
    assert record["source"] == {
        "name": "capsule",
        "digest": CapsuleSource(scenario["capsule"]["model"]).digest,
    }
    assert record["policy_digest"] == policy_digest(SimulationPolicy())
    assert record["deterministic"] is True
    assert spy[-1]["plan_ids"] == [plan_id]
    assert spy[-1]["policy"] == SimulationPolicy()
    assert workspace.repository.count("jobs") == 0, "no material-flow job any more"


def test_simulate_plan_scenario_plan_whose_source_changed_is_blocked(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service = make_service()
    plan_id = scenario_plan(workspace)
    plan = workspace.repository.get("schedule_plans", plan_id)
    snapshot = workspace.repository.get("schedule_snapshots", plan["schedule_snapshot_id"])
    snapshot["orders"][0]["quantity"] += 1
    workspace.repository.update("schedule_snapshots", snapshot["id"], snapshot)

    result = _row(_simulate(service, plan_id))

    assert result["status"] == "blocked"
    assert _codes(result) == ["schedule_source_changed"]
    assert spy == []
    assert workspace.repository.count("simulations") == 0


def test_simulate_plan_scenario_reps_are_capped_by_the_service(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service = make_service(max_simulation_replications=3)
    plan_id = scenario_plan(workspace)

    with pytest.raises(SizeLimit):
        _simulate(service, plan_id, reps=4)

    assert workspace.repository.count("simulations") == 0


# --- capability unavailable ---------------------------------------------------------------------


def test_simulate_tools_without_the_execution_engine_are_capability_unavailable(
    workspace: Workspace,
    make_service: Callable[..., PlanningService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile())
    portfolio_id = plan_id.rsplit(":", 2)[0]
    # `twinflow.erp.profile` needs `twinflow.execution.policy`; only the engine can go.
    monkeypatch.setitem(sys.modules, "twinflow.execution.evaluate", None)
    monkeypatch.setitem(sys.modules, "twinflow.execution.engine", None)

    with pytest.raises(CapabilityUnavailable):
        _simulate(service, plan_id)
    with pytest.raises(CapabilityUnavailable):
        service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER)

    assert _counts(workspace) == {"simulations": 0, "evaluations": 0}


# --- simulate_portfolio (D-108) -------------------------------------------------------------------

MENU_CANDIDATES = (
    ("00:base", 0, True),
    ("01:d1", 1, True),
    ("02:d2", 2, True),
    ("03:d3", 3, True),
    ("04:d4", 4, True),
    ("05:bad", 5, False),
)
MENU_ORDER = ("02:d2", "00:base")
RECOMMENDED = "03:d3"
D108_ORDER = ("03:d3", "02:d2", "00:base", "01:d1", "04:d4")


def _menu_portfolio(
    make_service: Callable[..., PlanningService],
    workspace: Workspace,
    profile: PlanningProfile | None = None,
    **limits: Any,
) -> tuple[PlanningService, str]:
    fake = MenuFake(MENU_CANDIDATES, menu=MENU_ORDER, recommended=RECOMMENDED)
    service = make_service(fake, **limits)
    built = service.build_problem(tiny_shop(), profile or tiny_profile())
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(built.problem_id, request_key="menu", caller_id=CALLER)
    for status in portfolio.statuses:
        assert _really_verifies(workspace, status.plan_id), status.plan_id  # precondition
    return service, portfolio.portfolio_id


def test_simulate_portfolio_orders_candidates_recommended_menu_then_by_id_up_to_the_cap(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace, max_simulation_candidates=3)
    expected = [f"{portfolio_id}:{cid}" for cid in D108_ORDER]

    result = _row(service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER))

    assert result["status"] == "completed", result["issues"]
    assert [c["plan_id"] for c in result["candidates"]] == expected[:3]
    assert len(spy) == 1
    assert spy[0]["plan_ids"] == expected[:3]
    capped = _issues_naming(result, "candidate_cap")
    assert len(capped) == 2
    for plan_id in expected[3:]:
        assert any(plan_id in text for text in capped), (plan_id, capped)
    unverified = _issues_naming(result, "candidate_unverified")
    assert len(unverified) == 1
    assert f"{portfolio_id}:05:bad" in unverified[0]


def test_simulate_portfolio_cap_is_the_min_of_policy_and_service(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    profile = _with_simulation(tiny_profile(), max_candidates=2)
    service, portfolio_id = _menu_portfolio(make_service, workspace, profile)

    result = _row(service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER))

    expected = [f"{portfolio_id}:{cid}" for cid in D108_ORDER[:2]]
    assert spy[0]["plan_ids"] == expected
    assert len(_issues_naming(result, "candidate_cap")) == 3


def test_simulate_portfolio_skips_a_candidate_that_fails_reverification(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace)
    tampered_id = f"{portfolio_id}:{RECOMMENDED}"
    record = workspace.repository.get("plans", tampered_id)
    del record["assignments"][0]
    workspace.repository.update("plans", tampered_id, record)

    result = _row(service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER))

    simulated = spy[0]["plan_ids"]
    assert tampered_id not in simulated
    assert simulated == [f"{portfolio_id}:{cid}" for cid in D108_ORDER[1:]]
    unverified = _issues_naming(result, "candidate_unverified")
    assert any(tampered_id in text for text in unverified), unverified


def test_simulate_portfolio_stores_a_simulation_per_candidate_and_baseline_plus_an_evaluation(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace, max_simulation_candidates=3)

    result = _row(service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER))

    assert EV_ID.match(result["evaluation_id"])
    assert result["portfolio_id"] == portfolio_id
    assert result["seed"] == 42
    assert result["validity"] == "provisional"
    assert result["evidence_kind"] == "portfolio_simulation"
    assert result["baseline"]["kind"] == "baseline"
    assert _counts(workspace) == {"simulations": 4, "evaluations": 1}
    evaluation = workspace.repository.get("evaluations", result["evaluation_id"])
    simulated = {c["plan_id"]: c["simulation_id"] for c in result["candidates"]}
    assert evaluation["id"] == result["evaluation_id"]
    assert evaluation["portfolio_id"] == portfolio_id
    assert (
        evaluation["problem_id"]
        == workspace.repository.get("portfolios", portfolio_id)["problem_id"]
    )
    assert evaluation["simulation_ids"] == simulated
    assert evaluation["baseline_simulation_id"] == result["baseline"]["simulation_id"]
    assert evaluation["summary"] == result
    assert evaluation["validity"] == "provisional"
    assert evaluation["evidence_kind"] == "portfolio_simulation"
    assert evaluation["created_at"]
    for plan_id, simulation_id in simulated.items():
        record = workspace.repository.get("simulations", simulation_id)
        assert record["plan_id"] == plan_id
        assert record["evaluation_id"] == result["evaluation_id"]


def test_simulate_portfolio_ranking_is_by_mean_simulated_objective(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace)

    result = _row(service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER))

    means = {
        c["plan_id"]: _mean_objectives(workspace.repository.get("simulations", c["simulation_id"]))
        for c in result["candidates"]
    }
    assert result["ranking"] == rank(means, tiny_profile().scheduling.objective)
    assert set(result["ranking"]) == set(means)


def test_simulate_portfolio_without_baseline_stores_no_baseline(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace, max_simulation_candidates=2)

    result = _row(
        service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER, baseline=False)
    )

    assert result["baseline"] is None
    assert spy[0]["baseline_rule"] is None
    assert _counts(workspace) == {"simulations": 2, "evaluations": 1}


def test_simulate_portfolio_replays_and_conflicts_like_every_keyed_call(
    workspace: Workspace, make_service: Callable[..., PlanningService], spy: list[dict[str, Any]]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace, max_simulation_candidates=2)

    first = service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER)
    again = service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER)
    with pytest.raises(IdempotencyConflict):
        service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER, seed=1)

    assert again == first
    assert len(spy) == 1
    assert _counts(workspace) == {"simulations": 3, "evaluations": 1}


def test_simulate_portfolio_reps_over_the_cap_is_size_limit_and_stores_nothing(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> None:
    service, portfolio_id = _menu_portfolio(make_service, workspace, max_simulation_replications=3)

    with pytest.raises(SizeLimit):
        service.simulate_portfolio(portfolio_id, request_key="pf", caller_id=CALLER, reps=4)

    assert _counts(workspace) == {"simulations": 0, "evaluations": 0}


def test_simulate_portfolio_unknown_portfolio_is_not_found(
    make_service: Callable[..., PlanningService],
) -> None:
    with pytest.raises(NotFound):
        make_service().simulate_portfolio("nope", request_key="pf", caller_id=CALLER)


def test_simulate_portfolio_on_the_small_shop_simulates_every_verified_candidate(
    workspace: Workspace, make_service: Callable[..., PlanningService], api: Any
) -> None:
    service = make_service(api)
    built = service.build_problem(small_shop(), erp_profile())
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(
        built.problem_id, request_key="solve", caller_id=CALLER, time_budget_seconds=10.0
    )
    verified = [s.plan_id for s in portfolio.statuses if s.verified]
    unverified = [s.plan_id for s in portfolio.statuses if not s.verified]

    result = _row(
        service.simulate_portfolio(portfolio.portfolio_id, request_key="pf", caller_id=CALLER)
    )

    assert result["status"] in {"completed", "truncated"}
    simulated = [c["plan_id"] for c in result["candidates"]]
    assert set(simulated) <= set(verified)
    assert len(simulated) == min(len(verified), 12)
    if portfolio.recommended_plan_id in verified:
        assert simulated[0] == portfolio.recommended_plan_id
    assert len(_issues_naming(result, "candidate_unverified")) == len(unverified)
    assert workspace.repository.count("evaluations") == 1


# --- compare_plans with simulations ---------------------------------------------------------------


@pytest.fixture
def two_plans(make_service: Callable[..., PlanningService]) -> tuple[PlanningService, str, str]:
    """Two verified plans of the tiny shop under the stochastic seed calibration."""
    service = make_service()
    built = service.build_problem(tiny_shop(), _stochastic(tiny_profile()))
    assert built.problem_id is not None
    portfolio = service.solve_portfolio(built.problem_id, request_key="solve", caller_id=CALLER)
    first, second = portfolio.plan_ids
    assert all(status.verified for status in portfolio.statuses)
    return service, first, second


def test_compare_plans_without_simulation_ids_has_no_simulation_block(
    two_plans: tuple[PlanningService, str, str],
) -> None:
    service, first, second = two_plans

    result = _row(service.compare_plans((first, second)))

    assert result["simulation"] is None


def test_compare_plans_with_simulations_gives_paired_deltas_and_simulated_ranking(
    workspace: Workspace, two_plans: tuple[PlanningService, str, str]
) -> None:
    service, first, second = two_plans
    sim_a = _row(_simulate(service, first, key="a", reps=4, seed=7))["simulation_id"]
    sim_b = _row(_simulate(service, second, key="b", reps=4, seed=7))["simulation_id"]
    record_a = workspace.repository.get("simulations", sim_a)
    record_b = workspace.repository.get("simulations", sim_b)

    block = _row(service.compare_plans((first, second), simulation_ids=(sim_a, sim_b)))[
        "simulation"
    ]

    assert block["seed"] == 7
    assert block["replications"] == 4
    assert block["reference_plan_id"] == first
    confidence = _stochastic(tiny_profile()).simulation.confidence_level
    for metric in METRIC_NAMES:
        expected = paired_delta(_per_rep(record_b, metric), _per_rep(record_a, metric), confidence)
        found = block["deltas"][second][metric]
        assert set(found) == DELTA_FIELDS
        assert math.isclose(found["mean_difference"], expected.mean_difference, abs_tol=1e-9)
        assert found["n"] == expected.n == 4
    means = {first: _mean_objectives(record_a), second: _mean_objectives(record_b)}
    assert block["ranking"] == rank(means, tiny_profile().scheduling.objective)


def test_compare_plans_reads_only(
    workspace: Workspace, two_plans: tuple[PlanningService, str, str]
) -> None:
    service, first, second = two_plans
    sim_a = _row(_simulate(service, first, key="a", reps=2))["simulation_id"]
    sim_b = _row(_simulate(service, second, key="b", reps=2))["simulation_id"]
    before = _counts(workspace)

    service.compare_plans((first, second), simulation_ids=(sim_a, sim_b))

    assert _counts(workspace) == before


def test_compare_plans_simulations_with_different_seeds_are_incomparable(
    two_plans: tuple[PlanningService, str, str],
) -> None:
    service, first, second = two_plans
    sim_a = _row(_simulate(service, first, key="a", reps=2, seed=7))["simulation_id"]
    sim_b = _row(_simulate(service, second, key="b", reps=2, seed=8))["simulation_id"]

    with pytest.raises(PlanningError) as raised:
        service.compare_plans((first, second), simulation_ids=(sim_a, sim_b))

    assert raised.value.code == "incomparable_simulations"


def test_compare_plans_simulation_of_another_plan_is_incomparable(
    two_plans: tuple[PlanningService, str, str],
) -> None:
    service, first, second = two_plans
    sim_a = _row(_simulate(service, first, key="a", reps=2))["simulation_id"]
    sim_b = _row(_simulate(service, second, key="b", reps=2))["simulation_id"]

    for ids in ((sim_a, sim_a), (sim_b, sim_a)):
        with pytest.raises(PlanningError) as raised:
            service.compare_plans((first, second), simulation_ids=ids)
        assert raised.value.code == "incomparable_simulations", ids


def test_compare_plans_unknown_simulation_is_not_found(
    two_plans: tuple[PlanningService, str, str],
) -> None:
    service, first, second = two_plans
    sim_a = _row(_simulate(service, first, key="a", reps=2))["simulation_id"]

    with pytest.raises(NotFound):
        service.compare_plans((first, second), simulation_ids=(sim_a, "sim-" + "0" * 32))


# --- simulation_evidence (REQ-141) ----------------------------------------------------------------


@pytest.fixture
def evidence_sim(
    workspace: Workspace, make_service: Callable[..., PlanningService]
) -> tuple[PlanningService, str, dict[str, Any]]:
    service = make_service()
    plan_id = _erp_plan(service, erp_profile(SEED_CALIBRATION))
    simulation_id = _row(_simulate(service, plan_id, reps=3))["simulation_id"]
    return service, simulation_id, workspace.repository.get("simulations", simulation_id)


def _page(service: PlanningService, simulation_id: str, **options: Any) -> dict[str, Any]:
    return _row(service.simulation_evidence(simulation_id, **options))


def test_simulation_evidence_pages_operation_rows_then_job_rows(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]],
) -> None:
    service, simulation_id, record = evidence_sim
    everything = [*record["operations"], *record["jobs"]]

    full = _page(service, simulation_id, limit=1000)

    assert full["simulation_id"] == simulation_id
    assert full["total"] == len(everything)
    assert full["rows"] == everything
    assert full["next_offset"] is None
    kinds = [row["row"] for row in full["rows"]]
    assert kinds == sorted(kinds, key=lambda kind: kind != "operation")
    assert {"operation", "job"} == set(kinds)


def test_simulation_evidence_offset_and_limit_walk_every_row_once(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]],
) -> None:
    service, simulation_id, record = evidence_sim
    everything = [*record["operations"], *record["jobs"]]
    rows: list[dict[str, Any]] = []
    offset: int | None = 0

    while offset is not None:
        page = _page(service, simulation_id, offset=offset, limit=7)
        assert page["offset"] == offset and page["limit"] == 7
        assert len(page["rows"]) <= 7
        rows.extend(page["rows"])
        offset = page["next_offset"]

    assert rows == everything


def test_simulation_evidence_row_shapes_follow_the_data_model(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]],
) -> None:
    service, simulation_id, _ = evidence_sim

    rows = _page(service, simulation_id, limit=1000)["rows"]

    op_fields = {
        "row", "plan_id", "operation_id", "job_id", "machine_id", "start_p10", "start_p50",
        "start_p90", "end_p10", "end_p50", "end_p90", "slip_mean", "slip_max", "causes",
        "overrun_seconds_mean", "max_lag_violations", "pin_window_misses", "censored_count",
    }  # fmt: skip
    job_fields = {
        "row", "plan_id", "job_id", "completion_p10", "completion_p50", "completion_p90",
        "lateness_p10", "lateness_p50", "lateness_p90", "on_time_share", "censored_count",
    }  # fmt: skip
    for row in rows:
        assert set(row) >= (op_fields if row["row"] == "operation" else job_fields), row


@pytest.mark.parametrize("entity", ["plan", "job", "operation", "machine"])
def test_simulation_evidence_filters_by_entity_id(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]], entity: str
) -> None:
    service, simulation_id, record = evidence_sim
    op = record["operations"][0]
    entity_id = {
        "plan": op["plan_id"],
        "job": op["job_id"],
        "operation": op["operation_id"],
        "machine": op["machine_id"],
    }[entity]
    everything = [*record["operations"], *record["jobs"]]
    keys = ("plan_id", "job_id", "operation_id", "machine_id")
    expected = [row for row in everything if entity_id in {row.get(key) for key in keys}]

    page = _page(service, simulation_id, entity_id=entity_id, limit=1000)

    assert page["rows"] == expected
    assert page["total"] == len(expected)
    if entity == "operation":
        assert [row["row"] for row in page["rows"]] == ["operation"]
    if entity == "job":
        assert {row["row"] for row in page["rows"]} == {"operation", "job"}
    if entity == "plan":
        assert page["rows"] == everything


def test_simulation_evidence_unknown_entity_gives_an_empty_page(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]],
) -> None:
    service, simulation_id, _ = evidence_sim

    page = _page(service, simulation_id, entity_id="no-such-entity")

    assert page["rows"] == []
    assert page["total"] == 0
    assert page["next_offset"] is None


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 1001}, {"offset": -1}])
def test_simulation_evidence_rejects_bad_paging(
    evidence_sim: tuple[PlanningService, str, dict[str, Any]], options: dict[str, Any]
) -> None:
    service, simulation_id, _ = evidence_sim

    with pytest.raises(ValueError):
        service.simulation_evidence(simulation_id, **options)


def test_simulation_evidence_unknown_simulation_is_not_found(
    make_service: Callable[..., PlanningService],
) -> None:
    with pytest.raises(NotFound):
        make_service().simulation_evidence("sim-" + "0" * 32)
