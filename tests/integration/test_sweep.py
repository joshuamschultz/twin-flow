"""COMP-024 SweepHarness — integration tests (T-040, red phase).

`SweepHarness` (`instrumentation/sweep.py`) runs a declared LEVER GRID — the
cartesian product of every lever's candidate values — and reports the KPIs of
EVERY resulting configuration side by side. It selects no winner and applies
no objective function (tech.md "Explicitly rejected": Optuna/OR-Tools/NSGA-II
rejected because no objective is defined; D-035, D-042). Sweep points pair by
REPLICATION INDEX for common random numbers (CRN); the pairing survives a
config change that alters the number of random draws, because
`engine.rng.RngRegistry` derives each source's stream from
`(base_seed, replication_index, source_index)` only — never from a draw count
or from anything point-specific (D-033). Output partitions by SWEEP POINT
ONLY (hive-partitioned dirs, ~one per point); replication is an ORDINARY
COLUMN inside each partition, never a partition key (SDD COMP-024's data
model section, "Storage layout").

--------------------------------------------------------------------------------
COMMITTED API (this test file fixes it; the T-041 implementer conforms):

    harness = SweepHarness(model_path: str)

    result = harness.run(
        plan: list[WorkOrder],
        sweep: dict[str, list[Any]],   # lever path -> candidate values
        reps: int,
        base_seed: int,
        out_dir: str | Path,
    )

`result` (name/class not asserted; this file only duck-types its attributes,
same "structural, not nominal" style `test_replication.py` already uses for
`RunResult`) exposes:

    result.points: list[dict[str, Any]]
        One dict per sweep point: {lever_path: assigned_value}. Length equals
        the product of each lever's candidate-value count. Every combination
        in the cartesian product appears exactly once; enumeration ORDER is
        not asserted by this file.

    result.point_ids: list[str]
        Parallel to `.points` (same length/order), one unique, filesystem-
        and hive-partition-safe string identifier per point (no "/", no "=").

    result.out_dir: Path
        Root of the partitioned output. For every `point_id` in
        `result.point_ids`, `out_dir / f"sweep_point={point_id}"` is a
        directory holding one or more `*.parquet` files whose union carries a
        `replication` column with exactly the values `0..reps-1`. No
        directory ANYWHERE under `out_dir` is itself partitioned by
        replication (no `replication=...` directory level) — replication
        never becomes a partition key.

    result.kpi_table: polars.DataFrame
        One row per (sweep point, replication): height == len(points) * reps.
        Carries at least the columns "sweep_point" (Utf8, values drawn from
        `point_ids`), "replication" (an integer dtype, values 0..reps-1 per
        sweep point), "run_hours" and "on_time_pct" (both already-real
        `instrumentation.kpis.KpiSet` scalar fields, forwarded per replication
        by the harness's own `KpiEngine` call).

    result.run_results: dict[str, list[RunResult]]
        point_id -> that point's `reps` `plan.driver.RunResult`s (the same
        type `ReplicationRunner.run()` already returns). Exists so a test
        (or a caller) can inspect `run_meta["seed"]` / `["replication_index"]`
        directly, without reaching into a spawned worker process.

--------------------------------------------------------------------------------
LEVER-OVERRIDE MECHANISM (this file's committed scheme; implementer conforms):

A lever path is a dot-separated string identifying one scalar field inside
the parsed `model.yaml` mapping (`model.loader.RawModel.data`'s shape), with
an optional `[selector]` suffix on any segment that names a list:

  * A bare segment (`"labor"`, `"time_model"`, `"rate"`) is a plain dict-key
    lookup.
  * A segment followed by `[selector]` (`"locations[cut]"`,
    `"pools[0]"`) first looks up the dict key, which must resolve to a
    list, then selects ONE item from it:
      - `selector` is all digits -> positional index into the list
        (`"pools[0]"` -> `data["labor"]["pools"][0]`).
      - Otherwise -> the first item whose own `"name"` key equals `selector`
        (`"locations[cut]"` -> the entry of `data["locations"]` whose
        `name == "cut"`).

Examples used by this file:

  * `"locations[cut].time_model.rate"` overrides the `cut` location's
    `time_model.rate`.
  * `"labor.pools[0].headcount"` overrides the first labor pool's headcount.

For each sweep point, the harness applies every declared override to a fresh
deep copy of the base model's parsed YAML, materializes a per-point model.yaml
variant, and runs it through the already-real `ReplicationRunner` (COMP-020)
at the SAME `base_seed` and `replication_index=0..reps-1` used by every other
point — never a point-derived or config-derived seed. That reuse is exactly
what "sweep points pair by replication index" means and exactly what proves
the pairing survives any config change, including one that would change how
many random draws a stochastic config makes: the seed derivation
(`RngRegistry`, already-shipped/D-033) never consults a draw count, and this
harness never substitutes a different `base_seed`/`replication_index` pair
per point.

--------------------------------------------------------------------------------
RED-PHASE IMPORT STRATEGY

`instrumentation/sweep.py` currently defines ONLY `SweepHarness`, whose
`__init__(self) -> None` takes no `model_path` argument and unconditionally
raises `NotImplementedError("T-041")`. Only `SweepHarness` (which does exist)
is imported at module level, so collection never raises `ImportError`. Every
test that constructs `SweepHarness(model_path)` fails today with
`TypeError: __init__() takes 1 positional argument but 2 were given` — a
signature mismatch, i.e. "feature absent" — the same valid RED precedent
`test_replication.py` documents for `ReplicationRunner.__init__(self) -> None`
and `test_run_driver.py` documents for `RunDriver.__init__(self) -> None`.

Real collaborators throughout: real YAML on disk, the real (already-shipped)
`load_model`/`RunDriver`/`ReplicationRunner`/`KpiEngine`, real SimPy, real
Polars/Parquet round-trips. No mocks anywhere in this file — every assertion
is made by reading the harness's own returned artifacts back in this process.
"""

from __future__ import annotations

import inspect
import itertools
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from twinflow.instrumentation.sweep import SweepHarness
from twinflow.plan.loader import WorkOrder

# ---------------------------------------------------------------------------
# Fixture vocabulary — same minimal cut -> pack shape
# `tests/integration/test_replication.py` already proved works end to end,
# parameterized on the `cut` location's rate and on labor headcount so two
# distinguishable levers exist to sweep.
# ---------------------------------------------------------------------------

ORDER_QTY = 2


def _model_yaml(cut_rate: float = 10.0, headcount: int = 2) -> str:
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
      headcount: {headcount}
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


def _write_model(dir_path: Path, cut_rate: float = 10.0, headcount: int = 2) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    model_path = dir_path / "model.yaml"
    model_path.write_text(_model_yaml(cut_rate=cut_rate, headcount=headcount), encoding="utf-8")
    return model_path


def _plan(qty: int = ORDER_QTY) -> list[WorkOrder]:
    """One demand order for `finished`, released at t=0. Kept minimal so grid
    x reps stays small and fast (per-task instruction: 2 levers x 2 values x
    2 reps)."""
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
    """Run inside tmp_path so any auto-generated `runs/<run-id>/` output (from
    the underlying `ReplicationRunner`/`RunDriver`) never touches the real
    repository tree, exactly as `test_replication.py`'s fixture of the same
    name does."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _expected_point_set(sweep: dict[str, list[Any]]) -> set[frozenset[tuple[str, Any]]]:
    """The cartesian product of `sweep`'s lever values, as an order-independent
    set of frozensets, for comparing against `result.points` without assuming
    a specific enumeration order."""
    keys = list(sweep)
    combos = itertools.product(*(sweep[key] for key in keys))
    return {frozenset(zip(keys, combo, strict=True)) for combo in combos}


# ---------------------------------------------------------------------------
# Import sanity
# ---------------------------------------------------------------------------


def test_sweep_module_imports_cleanly() -> None:
    assert isinstance(SweepHarness, type)


# ---------------------------------------------------------------------------
# Acceptance 1 — a declared lever grid runs EVERY point: the number of sweep
# points equals the product of each lever's value count, and every
# combination in the cartesian product is covered exactly once.
# ---------------------------------------------------------------------------


def test_sweep_runs_every_point_in_the_cartesian_grid(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    sweep = {
        "locations[cut].time_model.rate": [10.0, 20.0],
        "labor.pools[0].headcount": [1, 2],
    }
    harness = SweepHarness(str(model_path))

    result = harness.run(
        plan, sweep=sweep, reps=2, base_seed=11, out_dir=isolated_cwd / "sweep_out"
    )

    assert len(result.points) == 4, (
        f"2 lever values x 2 lever values must produce 2*2=4 sweep points, got {len(result.points)}"
    )
    assert len(result.point_ids) == len(result.points)
    assert len(set(result.point_ids)) == len(result.point_ids), "point_ids must be unique"

    observed = {frozenset(point.items()) for point in result.points}
    assert observed == _expected_point_set(sweep), (
        "every combination in the cartesian product must be covered exactly once"
    )

    # Every declared sweep point actually produced replication results.
    assert set(result.run_results.keys()) == set(result.point_ids)
    for point_id in result.point_ids:
        assert len(result.run_results[point_id]) == 2, (
            f"sweep point {point_id!r} must have produced exactly `reps` results"
        )


# ---------------------------------------------------------------------------
# Acceptance 2 — sweep points pair by replication index for CRN; the pairing
# survives a config change that alters draw behavior, because the harness
# reuses the SAME base_seed and the SAME replication_index sequence
# (0..reps-1) at every point, never a point-derived or config-derived seed.
# ---------------------------------------------------------------------------


def test_sweep_pairs_replication_index_with_identical_base_seed_across_differing_configs(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    base_seed = 21
    reps = 2
    # Two levers whose values meaningfully change what each point's config
    # does (cut rate changes cycle time entirely; headcount changes labor
    # concurrency) — a stand-in for "a config change that alters the number
    # of random draws" a stochastic config would make.
    sweep = {
        "locations[cut].time_model.rate": [5.0, 40.0],
        "labor.pools[0].headcount": [1, 2],
    }
    harness = SweepHarness(str(model_path))

    result = harness.run(
        plan, sweep=sweep, reps=reps, base_seed=base_seed, out_dir=isolated_cwd / "sweep_out"
    )

    assert len(result.run_results) == 4, "expected all 4 sweep points to have run"

    for point_id, run_results in result.run_results.items():
        seeds = {int(r.run_meta["seed"]) for r in run_results}
        assert seeds == {base_seed}, (
            f"sweep point {point_id!r}: every replication must carry the SAME "
            f"base_seed ({base_seed}) regardless of that point's config, "
            f"got {seeds}"
        )
        indices = sorted(int(r.run_meta["replication_index"]) for r in run_results)
        assert indices == list(range(reps)), (
            f"sweep point {point_id!r}: replication_index must run 0..{reps - 1} "
            f"exactly once each, got {indices}"
        )

    # The set of (seed, replication_index) pairs used at replication index i
    # must be identical across every point — one pair per index, reused by
    # every point — never a distinct pair per point. This is the literal
    # mechanism by which CRN pairing survives a config change: nothing about
    # a point's config or draw count ever feeds into the seed derivation.
    for index in range(reps):
        pairs_at_index = {
            (int(r.run_meta["seed"]), int(r.run_meta["replication_index"]))
            for run_results in result.run_results.values()
            for r in run_results
            if int(r.run_meta["replication_index"]) == index
        }
        assert pairs_at_index == {(base_seed, index)}, (
            f"replication index {index}: expected exactly one (seed, index) pair "
            f"{(base_seed, index)} reused by every sweep point, got {pairs_at_index}"
        )


# ---------------------------------------------------------------------------
# Adversarial companion to Acceptance 2 — proves an override actually reaches
# the compiled model (not merely accepted and ignored) by observing a real
# KPI difference between two points that only differ in one lever's value.
# ---------------------------------------------------------------------------


def test_sweep_lever_override_actually_changes_the_compiled_model(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    sweep = {"locations[cut].time_model.rate": [2.0, 50.0]}
    harness = SweepHarness(str(model_path))

    result = harness.run(plan, sweep=sweep, reps=1, base_seed=7, out_dir=isolated_cwd / "sweep_out")

    run_hours_by_rate = {
        point["locations[cut].time_model.rate"]: result.kpi_table.filter(
            pl.col("sweep_point") == point_id
        )["run_hours"].to_list()[0]
        for point, point_id in zip(result.points, result.point_ids, strict=True)
    }
    assert run_hours_by_rate[2.0] != run_hours_by_rate[50.0], (
        "overriding locations[cut].time_model.rate must actually change the "
        "compiled model's cycle time, not be silently dropped"
    )
    # A slower cut rate (2.0) must take strictly more machine-hours than a
    # faster one (50.0) for the same order quantity — rate_based run time is
    # qty / rate, so this is the correct direction, not just "different".
    assert run_hours_by_rate[2.0] > run_hours_by_rate[50.0]


# ---------------------------------------------------------------------------
# Acceptance 3 — output partitions by sweep point ONLY (hive-partitioned
# dirs), with replication as an ordinary column; the KPI table has one row
# per (sweep point, replication).
# ---------------------------------------------------------------------------


def test_sweep_output_partitions_by_sweep_point_only_with_replication_as_a_column(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    sweep = {
        "locations[cut].time_model.rate": [10.0, 20.0],
        "labor.pools[0].headcount": [1, 2],
    }
    reps = 2
    out_dir = isolated_cwd / "sweep_out"
    harness = SweepHarness(str(model_path))

    result = harness.run(plan, sweep=sweep, reps=reps, base_seed=31, out_dir=out_dir)

    assert result.out_dir == Path(out_dir) or Path(result.out_dir) == Path(out_dir)

    # One hive-style "sweep_point=<id>" directory per point, no more, no less.
    child_dirs = {p.name for p in Path(out_dir).iterdir() if p.is_dir()}
    expected_dirs = {f"sweep_point={point_id}" for point_id in result.point_ids}
    assert child_dirs == expected_dirs, (
        f"expected exactly one 'sweep_point=<id>' directory per point, "
        f"got {child_dirs} vs expected {expected_dirs}"
    )

    # Replication is NEVER its own partition/directory level, anywhere.
    for path in Path(out_dir).rglob("*"):
        if path.is_dir():
            assert not path.name.startswith("replication="), (
                f"found a replication-level partition directory {path}: "
                "replication must be an ordinary column, never a partition key"
            )

    # Each point's partition dir holds parquet data whose `replication`
    # column covers exactly 0..reps-1.
    for point_id in result.point_ids:
        partition_dir = Path(out_dir) / f"sweep_point={point_id}"
        parquet_files = sorted(partition_dir.glob("*.parquet"))
        assert parquet_files, f"sweep point {point_id!r} wrote no parquet output"
        combined = pl.read_parquet([str(p) for p in parquet_files])
        assert "replication" in combined.columns, (
            f"sweep point {point_id!r}: partition parquet must carry an "
            "ordinary 'replication' column"
        )
        assert set(combined["replication"].unique().to_list()) == set(range(reps))

    # KPI table: one row per (sweep_point, replication).
    kpi_table = result.kpi_table
    assert kpi_table.height == len(result.point_ids) * reps
    assert {"sweep_point", "replication"}.issubset(set(kpi_table.columns))
    assert set(kpi_table["sweep_point"].unique().to_list()) == set(result.point_ids)
    for point_id in result.point_ids:
        reps_for_point = sorted(
            kpi_table.filter(pl.col("sweep_point") == point_id)["replication"].to_list()
        )
        assert reps_for_point == list(range(reps)), (
            f"sweep point {point_id!r}: kpi_table must have exactly one row "
            f"per replication 0..{reps - 1}, got {reps_for_point}"
        )


# ---------------------------------------------------------------------------
# Acceptance 4 — the harness selects NO winner and applies NO objective
# function: no `.best`/`.winner`/`.recommended` on the result, and `.run()`
# offers no objective/scoring parameter.
# ---------------------------------------------------------------------------


def test_sweep_result_exposes_no_winner_attribute(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    sweep = {"locations[cut].time_model.rate": [10.0, 20.0]}
    harness = SweepHarness(str(model_path))

    result = harness.run(plan, sweep=sweep, reps=1, base_seed=3, out_dir=isolated_cwd / "sweep_out")

    for forbidden in ("best", "winner", "recommended", "optimal", "top", "chosen"):
        assert not hasattr(result, forbidden), (
            f"SweepHarness result must not expose '.{forbidden}' — the harness "
            "selects no winner and applies no objective function (D-035, D-042)"
        )


def test_sweep_run_signature_offers_no_objective_or_scoring_parameter() -> None:
    forbidden_names = {"objective", "metric", "scoring", "score_fn", "rank_by", "weight"}
    params = set(inspect.signature(SweepHarness.run).parameters)
    overlap = params & forbidden_names
    assert not overlap, (
        f"SweepHarness.run() must not accept an objective/scoring parameter; "
        f"found {overlap} in signature {params}"
    )


def test_sweep_run_rejects_an_unexpected_objective_keyword_argument(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    harness = SweepHarness(str(model_path))

    with pytest.raises(TypeError):
        harness.run(
            plan,
            sweep={"locations[cut].time_model.rate": [10.0, 20.0]},
            reps=1,
            base_seed=3,
            out_dir=isolated_cwd / "sweep_out",
            objective=lambda *_: 0.0,  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Adversarial — a lever declared with zero candidate values would make the
# cartesian product empty (a silent no-op sweep); fail closed instead,
# matching the project's fail-closed convention elsewhere (plan/loader.py
# raises ValueError rather than silently skipping bad rows).
# ---------------------------------------------------------------------------


def test_sweep_rejects_a_lever_with_an_empty_value_list(isolated_cwd: Path) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    harness = SweepHarness(str(model_path))

    with pytest.raises(ValueError):
        harness.run(
            plan,
            sweep={"locations[cut].time_model.rate": []},
            reps=1,
            base_seed=3,
            out_dir=isolated_cwd / "sweep_out",
        )


# ---------------------------------------------------------------------------
# Boundary — a degenerate 1-point x 1-replication sweep still partitions and
# reports correctly; the multi-point/multi-rep machinery has no special-cased
# minimum size below which it silently breaks.
# ---------------------------------------------------------------------------


def test_sweep_with_a_single_point_and_single_replication_still_partitions_and_reports(
    isolated_cwd: Path,
) -> None:
    model_path = _write_model(isolated_cwd)
    plan = _plan()
    harness = SweepHarness(str(model_path))

    result = harness.run(
        plan,
        sweep={"locations[cut].time_model.rate": [10.0]},
        reps=1,
        base_seed=3,
        out_dir=isolated_cwd / "sweep_out",
    )

    assert len(result.points) == 1
    assert len(result.point_ids) == 1
    assert result.kpi_table.height == 1

    (point_id,) = result.point_ids
    partition_dir = Path(isolated_cwd / "sweep_out") / f"sweep_point={point_id}"
    assert partition_dir.is_dir()
    assert list(partition_dir.glob("*.parquet")), "the single point must still write parquet output"
