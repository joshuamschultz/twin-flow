"""COMP-020 ReplicationRunner — integration tests (T-038, red phase).

`ReplicationRunner` (`plan/replication.py`) runs N replications of ONE
configuration in PARALLEL, one replication per OS process via
`multiprocessing.Pool` (D-028, tech.md), and reports a t-based confidence
interval (SciPy `stats.t`) across them. A paired comparison of two configs is
a single CI on the per-replication-index DIFFERENCE, never two overlapping
CIs (SDD COMP-020).

--------------------------------------------------------------------------------
THE PICKLING CONSTRAINT THIS DESIGN IS BUILT AROUND

A `CompiledModel` (`model/__init__.py`) carries `LocationSpec`s whose
`OutputSpec.qty`/emit expressions are `simpleeval`-wrapped Python closures
(`model/compile.py`), and macOS `multiprocessing` defaults to the `spawn`
start method, which pickles every argument handed to a worker. A closure
never pickles. Every test below therefore hands `ReplicationRunner` a MODEL
PATH (`str`), never a `CompiledModel` instance, and every assertion about
"what ran in the worker" is made by reading the worker's OWN output artifacts
back in this process (the `RunResult`s `ReplicationRunner.run()` returns, and
the Parquet files they point at) — never by trying to pickle a closure or a
live SimPy object across the process boundary. `plan.loader.WorkOrder` is a
plain picklable `@dataclass`, so `plan` is passed through unchanged; the
implementer's per-process worker function is expected to be MODULE-LEVEL
(spawn requires the target to be importable by qualified name) and to call
`load_model(model_path)` itself, fresh, inside each worker.

--------------------------------------------------------------------------------
COMMITTED API (this test file fixes it; the T-039 implementer conforms):

    runner = ReplicationRunner(model_path: str)
    results: list[RunResult] = runner.run(plan: list[WorkOrder], reps: int, base_seed: int)
        # one multiprocessing.Pool, one replication per process; replication i
        # uses replication_index=i (CRN, D-033); RunResult.run_meta already
        # carries "seed" and "replication_index" (plan/driver.py, unchanged).

    confidence_interval(values: Sequence[float], confidence: float = 0.95) -> tuple[float, float]
        # a plain-float (low, high) CI on the mean of `values`, t-quantile via
        # scipy.stats.t (tech.md: "Polars has no t-distribution quantile").

    compare(
        model_path_a: str, model_path_b: str, plan: list[WorkOrder],
        reps: int, base_seed: int, metric: Callable[[RunResult], float],
        confidence: float = 0.95,
    ) -> DifferenceCI
        # runs `reps` replications of EACH config at the SAME base_seed (so
        # replication i of A pairs with replication i of B), and returns ONE
        # interval on mean(metric(B_i) - metric(A_i)) — never a pair of
        # per-config intervals.

    @dataclass(frozen=True)
    class DifferenceCI:
        mean_difference: float
        low: float
        high: float
        t_critical: float   # a plain Python float, per COMP-020's SciPy contract

--------------------------------------------------------------------------------
RED-PHASE IMPORT STRATEGY

`plan/replication.py` currently defines ONLY `ReplicationRunner`, whose
`__init__(self) -> None` takes no `model_path` argument and unconditionally
raises `NotImplementedError("T-039")`. `compare`, `confidence_interval` and
`DifferenceCI` do not exist yet at all. Importing them at MODULE level would
raise `ImportError` and fail collection for the WHOLE file — the wrong RED
reason. So only `ReplicationRunner` (which does exist) is imported at module
level; every test that needs `compare`/`confidence_interval`/`DifferenceCI`
imports them locally inside the test body, so a missing-name failure is
scoped to that one test, not the whole module. Every test that constructs
`ReplicationRunner(model_path)` fails today with `TypeError: __init__() takes
1 positional argument but 2 were given` — a signature mismatch, i.e. "feature
absent", the same valid RED precedent `test_run_driver.py` already
documents for `RunDriver.__init__(self) -> None`.

Real collaborators throughout: real YAML on disk, the real (already-shipped)
`load_model`/`RunDriver`, real SimPy, real Polars/Parquet round-trips. The
only mock is `multiprocessing.Pool` (wrapped, not replaced) in one test, to
prove the parallelism mechanism is genuinely used — the same external-process
boundary `test_run_driver.py` already mocks (there, to prove it is NOT used
by a single replication; here, to prove it IS used across replications).
"""

from __future__ import annotations

import math
import multiprocessing
import statistics
from pathlib import Path
from unittest import mock

import numpy as np
import polars as pl
import pytest
import scipy.stats

from twinflow.instrumentation.event_log import PROCESS_NAMES
from twinflow.model import load_model
from twinflow.plan.driver import RunDriver, RunResult
from twinflow.plan.loader import WorkOrder
from twinflow.plan.replication import ReplicationRunner

# ---------------------------------------------------------------------------
# Fixture vocabulary — same shape as tests/integration/test_run_driver.py's
# proven-working model, parameterized on the `cut` location's rate so two
# distinguishable configs can be built for the paired-comparison test.
# ---------------------------------------------------------------------------

ORDER_QTY = 2


def _model_yaml(cut_rate: float) -> str:
    return f"""
stocks: []

part_types:
  - name: raw_wire
    uom: ft
    attributes: {{}}
  - name: blank
    uom: piece
    attributes: {{}}
  - name: finished
    uom: piece
    attributes: {{}}

machines:
  - name: cut_m
  - name: pack_m

labor:
  pools:
    - name: floor_pool
      headcount: 2
      skills: [cut_op, pack_op]

locations:
  - name: cut
    consumes:
      - thing: raw_wire
        qty: 1
        uom: ft
    emits:
      - thing: blank
        qty: 1
        uom: piece
    setup_key: grp_cut
    time_model:
      kind: rate_based
      rate: {cut_rate}
    machine: cut_m
    labor_skill: cut_op

  - name: pack
    consumes:
      - thing: blank
        qty: 1
        uom: piece
    emits:
      - thing: finished
        qty: 1
        uom: piece
    setup_key: grp_pack
    time_model:
      kind: rate_based
      rate: 10
    machine: pack_m
    labor_skill: pack_op

routing:
  - part: finished
    steps: [cut, pack]

processes: []
bom: []
"""


def _write_model(dir_path: Path, cut_rate: float = 10.0) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    model_path = dir_path / "model.yaml"
    model_path.write_text(_model_yaml(cut_rate), encoding="utf-8")
    return model_path


def _plan(qty: int = ORDER_QTY) -> list[WorkOrder]:
    """One demand order for `finished`, released at t=0. No initial WIP —
    kept minimal so replication counts stay small and fast (reps 2-4)."""
    return [
        WorkOrder(
            work_order_id="wo-A",
            part="finished",
            qty=qty,
            start_date="0",
            due_date="100000",
        )
    ]


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run inside tmp_path so RunDriver's auto-generated `runs/<run-id>/`
    output directory never touches the real repository tree. `os.chdir` here
    changes the actual OS process cwd, which spawned multiprocessing workers
    inherit like any subprocess — no extra plumbing needed for that to hold
    across the process boundary."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _by_replication_index(results: list[RunResult]) -> dict[int, RunResult]:
    return {int(result.run_meta["replication_index"]): result for result in results}


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_replication_module_imports_cleanly() -> None:
    assert isinstance(ReplicationRunner, type)


# ---------------------------------------------------------------------------
# Acceptance 1 — N replications run in parallel via multiprocessing.Pool, one
# per process, and reproduce the same results as running them serially.
# ---------------------------------------------------------------------------


def test_replication_runner_dispatches_via_multiprocessing_pool(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    runner = ReplicationRunner(str(model_path))

    with mock.patch(
        "multiprocessing.get_context", wraps=multiprocessing.get_context
    ) as context_spy:
        results = runner.run(plan, reps=3, base_seed=11)

    (
        context_spy.assert_called_once_with("spawn"),
        (
            "ReplicationRunner.run() must dispatch replications through "
            "an explicit spawn multiprocessing context, one replication per process (D-028)"
        ),
    )
    assert len(results) == 3


def test_run_replications_matches_serial_run_at_same_base_seed_and_plan(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    reps = 3
    base_seed = 11

    runner = ReplicationRunner(str(model_path))
    parallel_results = runner.run(plan, reps=reps, base_seed=base_seed)
    parallel_by_index = _by_replication_index(parallel_results)
    assert set(parallel_by_index) == set(range(reps)), (
        f"expected replication_index 0..{reps - 1} exactly once each, "
        f"got {sorted(parallel_by_index)}"
    )

    # Serial baseline: drive RunDriver directly in THIS process, never
    # through ReplicationRunner, at the same seed/replication_index pairs.
    compiled = load_model(str(model_path))
    driver = RunDriver(compiled)
    serial_by_index = {
        i: driver.run(plan, seed=base_seed, replication_index=i) for i in range(reps)
    }

    for index in range(reps):
        parallel_result = parallel_by_index[index]
        serial_result = serial_by_index[index]
        assert parallel_result.horizon == pytest.approx(serial_result.horizon), (
            f"replication {index}: parallel horizon diverged from serial"
        )
        df_parallel = pl.read_parquet(parallel_result.event_log_path)
        df_serial = pl.read_parquet(serial_result.event_log_path)
        assert df_parallel.equals(df_serial), (
            f"replication {index}: parallel event log diverged from serial event log"
        )


# ---------------------------------------------------------------------------
# Acceptance 2 — every worker writes the same Enum categories; parquet files
# from different reps scan together with no SchemaError.
# ---------------------------------------------------------------------------


def test_worker_parquet_files_share_identical_process_name_categories_and_scan_together(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    runner = ReplicationRunner(str(model_path))

    results = runner.run(plan, reps=3, base_seed=5)
    paths = [str(result.event_log_path) for result in results]
    assert len(paths) == 3

    for path in paths:
        schema = pl.scan_parquet(path).collect_schema()
        assert schema["process_name"] == PROCESS_NAMES, (
            f"{path}: process_name dtype must be the one shared PROCESS_NAMES "
            "Enum, applied identically in every worker process (D-017)"
        )

    # A combined multi-file scan across independently-spawned processes must
    # not raise SchemaError/ComputeError from a category mismatch.
    combined = pl.scan_parquet(paths).collect()
    assert combined.height > 0
    assert combined.schema["process_name"] == PROCESS_NAMES


# ---------------------------------------------------------------------------
# Acceptance 3 — a paired comparison of two configs is a single CI on the
# DIFFERENCE (paired by replication index), never two overlapping CIs.
# ---------------------------------------------------------------------------


def test_compare_returns_a_single_paired_difference_ci_not_two_overlapping_cis(
    isolated_cwd: Path,
) -> None:
    from twinflow.plan.replication import DifferenceCI, compare

    model_a = _write_model(isolated_cwd / "config_a", cut_rate=10.0)
    model_b = _write_model(isolated_cwd / "config_b", cut_rate=5.0)
    plan = _plan()
    reps = 4
    base_seed = 21

    def horizon_metric(result: RunResult) -> float:
        return result.horizon

    result = compare(
        str(model_a),
        str(model_b),
        plan,
        reps=reps,
        base_seed=base_seed,
        metric=horizon_metric,
    )

    # A single interval on the difference — never a pair of per-config CIs.
    assert isinstance(result, DifferenceCI)
    assert not hasattr(result, "ci_a")
    assert not hasattr(result, "ci_b")
    assert not hasattr(result, "interval_a")
    assert not hasattr(result, "interval_b")
    assert result.low <= result.mean_difference <= result.high

    # The t critical value is a plain Python float from scipy.stats.t, never
    # a numpy scalar leaking out of the boundary.
    assert type(result.t_critical) is float
    assert not isinstance(result.t_critical, np.generic)

    # Independently recompute the expected paired-by-index difference CI
    # using RunDriver directly (never through ReplicationRunner), to prove
    # `compare` pairs replication i of A with replication i of B and uses a
    # real SciPy t-quantile rather than a made-up multiplier.
    compiled_a = load_model(str(model_a))
    compiled_b = load_model(str(model_b))
    driver_a = RunDriver(compiled_a)
    driver_b = RunDriver(compiled_b)
    diffs = [
        driver_b.run(plan, seed=base_seed, replication_index=i).horizon
        - driver_a.run(plan, seed=base_seed, replication_index=i).horizon
        for i in range(reps)
    ]

    expected_mean_diff = statistics.fmean(diffs)
    expected_t_critical = float(scipy.stats.t.ppf(0.975, df=reps - 1))
    if statistics.pstdev(diffs) == 0.0:
        expected_stderr = 0.0
    else:
        expected_stderr = statistics.stdev(diffs) / math.sqrt(reps)
    expected_low = expected_mean_diff - expected_t_critical * expected_stderr
    expected_high = expected_mean_diff + expected_t_critical * expected_stderr

    assert result.mean_difference == pytest.approx(expected_mean_diff, abs=1e-6)
    assert result.t_critical == pytest.approx(expected_t_critical, rel=1e-6)
    assert result.low == pytest.approx(expected_low, abs=1e-6)
    assert result.high == pytest.approx(expected_high, abs=1e-6)


# ---------------------------------------------------------------------------
# Acceptance 4 — determinism: the same (base_seed, reps) yields identical
# results across two runner.run() calls.
# ---------------------------------------------------------------------------


def test_two_runner_run_calls_at_same_base_seed_produce_identical_results(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    runner = ReplicationRunner(str(model_path))

    first_by_index = _by_replication_index(runner.run(plan, reps=2, base_seed=99))
    second_by_index = _by_replication_index(runner.run(plan, reps=2, base_seed=99))

    assert set(first_by_index) == set(second_by_index) == {0, 1}
    for index in (0, 1):
        first_result = first_by_index[index]
        second_result = second_by_index[index]
        assert first_result.horizon == pytest.approx(second_result.horizon)
        df_first = pl.read_parquet(first_result.event_log_path)
        df_second = pl.read_parquet(second_result.event_log_path)
        assert df_first.equals(df_second), (
            f"replication {index}: two runner.run() calls at the same "
            "(base_seed, reps) produced different event logs"
        )


# ---------------------------------------------------------------------------
# Adversarial — base_seed and replication_index must actually be threaded
# through to every worker, never silently hardcoded or defaulted.
# ---------------------------------------------------------------------------


def test_run_meta_reports_seed_and_sequential_replication_index_per_worker(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    runner = ReplicationRunner(str(model_path))

    results = runner.run(plan, reps=3, base_seed=42)

    seeds = {int(result.run_meta["seed"]) for result in results}
    assert seeds == {42}, (
        "every replication's run_meta must carry the shared base_seed, "
        f"not a per-worker default or hardcoded value; got {seeds}"
    )

    indices = sorted(int(result.run_meta["replication_index"]) for result in results)
    assert indices == [0, 1, 2], (
        "replication_index must run i=0..reps-1, exactly once each, one per "
        f"process (CRN, D-033); got {indices}"
    )
