"""COMP-024 SweepHarness — a declared lever grid with CRN pairing across sweep points.

Runs the cartesian product of a declared lever grid and reports the KPIs of every
resulting configuration side by side. Selects no winner and applies no objective
function (D-035, D-042: Optuna/OR-Tools/NSGA-II rejected because no objective is
defined for v1 — grid/random sweep with KPIs reported side by side, instead).

Every sweep point runs `reps` replications at the SAME `base_seed` and
`replication_index=0..reps-1` sequence via the already-shipped `ReplicationRunner`
(COMP-020), so replications pair by index across points for common random numbers
(CRN, D-033) — the pairing survives a config change that alters draw counts, because
`RngRegistry` derives each stream from `(base_seed, replication_index, source_index)`
only, never from anything point-specific or draw-count-derived.

Output partitions by SWEEP POINT ONLY (hive-style `sweep_point=<id>` directories);
replication is an ordinary column inside each partition's parquet, never its own
directory level (SDD COMP-024 "Storage layout").
"""

from __future__ import annotations

import copy
import json
import re
import tempfile
from dataclasses import dataclass
from itertools import product
from pathlib import Path
from typing import Any, cast

import polars as pl

from factory_twin.instrumentation.kpis import KpiEngine
from factory_twin.model import CompiledModel, load_model
from factory_twin.model.loader import load_raw_model
from factory_twin.plan.driver import RunResult
from factory_twin.plan.loader import WorkOrder
from factory_twin.plan.replication import ReplicationRunner

# A dot-path segment is a bare dict key, optionally followed by `[selector]`
# selecting one item from the list that key resolves to (see module docstring
# of tests/integration/test_sweep.py for the committed scheme this mirrors).
_SEGMENT_RE = re.compile(r"^([^\[\]]+)(?:\[(.+)\])?$")

_ORDERS_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "order_id": pl.Utf8,
    "part_id": pl.Utf8,
    "due_date": pl.Float64,
    "lot_id": pl.Utf8,
}


@dataclass(frozen=True)
class SweepResult:
    """`SweepHarness.run()`'s return value. Duck-typed by callers/tests, same
    "structural, not nominal" convention `plan.driver.RunResult` already uses."""

    points: list[dict[str, Any]]
    point_ids: list[str]
    out_dir: Path
    kpi_table: pl.DataFrame
    run_results: dict[str, list[RunResult]]


class SweepHarness:
    """Runs a declared lever grid: for every point in the cartesian product of
    `sweep`'s candidate values, applies the lever overrides to a fresh copy of
    the base model, runs `reps` replications, and reports KPIs side by side.
    """

    def __init__(self, model_path: str) -> None:
        self._model_path = model_path

    def run(
        self,
        plan: list[WorkOrder],
        sweep: dict[str, list[Any]],
        reps: int,
        base_seed: int,
        out_dir: str | Path,
    ) -> SweepResult:
        """Run every point in `sweep`'s cartesian product, `reps` replications
        each, all at `base_seed` (CRN, D-033). Fails closed (`ValueError`) on a
        lever declared with zero candidate values, matching the project's
        fail-closed convention elsewhere (`plan/loader.py`).
        """
        for lever_path, values in sweep.items():
            if not values:
                raise ValueError(f"lever {lever_path!r} declares an empty candidate-value list")

        points = _cartesian_points(sweep)
        point_ids = _point_ids(points)

        out_dir_path = Path(out_dir)
        out_dir_path.mkdir(parents=True, exist_ok=True)

        base_data = cast(dict[str, Any], load_raw_model(self._model_path).data)

        run_results: dict[str, list[RunResult]] = {}
        kpi_rows: list[dict[str, Any]] = []

        with tempfile.TemporaryDirectory(prefix="sweep-variant-") as tmp_dir:
            for point, point_id in zip(points, point_ids, strict=True):
                variant_path = _write_variant(base_data, point, Path(tmp_dir), point_id)
                compiled = load_model(str(variant_path))

                results = sorted(
                    ReplicationRunner(str(variant_path)).run(plan, reps, base_seed),
                    key=lambda result: cast(int, result.run_meta["replication_index"]),
                )
                run_results[point_id] = results

                kpi_rows.extend(_kpi_rows_for_point(point_id, plan, compiled, results))
                _write_partition(out_dir_path, point_id, results)

        kpi_table = pl.DataFrame(kpi_rows)
        return SweepResult(
            points=points,
            point_ids=point_ids,
            out_dir=out_dir_path,
            kpi_table=kpi_table,
            run_results=run_results,
        )


# ---------------------------------------------------------------------------
# Cartesian grid + point identifiers
# ---------------------------------------------------------------------------


def _cartesian_points(sweep: dict[str, list[Any]]) -> list[dict[str, Any]]:
    """The cartesian product of `sweep`'s candidate values, as one lever-path ->
    assigned-value dict per point. Enumeration order is not part of the
    committed contract (only coverage and uniqueness are)."""
    keys = list(sweep)
    combos = product(*(sweep[key] for key in keys))
    return [dict(zip(keys, combo, strict=True)) for combo in combos]


def _point_ids(points: list[dict[str, Any]]) -> list[str]:
    """One unique, filesystem- and hive-partition-safe id per point (no "/",
    no "="). Sequential and content-independent: uniqueness must hold even
    when two points differ only in a value type that has no stable string
    form."""
    return [f"point-{index:04d}" for index in range(len(points))]


# ---------------------------------------------------------------------------
# Lever-override mechanism
# ---------------------------------------------------------------------------


def _parse_segment(segment: str) -> tuple[str, str | None]:
    match = _SEGMENT_RE.match(segment)
    if match is None:
        raise ValueError(f"malformed lever-path segment: {segment!r}")
    return match.group(1), match.group(2)


def _select_list_item(items: list[Any], selector: str) -> Any:
    if selector.isdigit():
        return items[int(selector)]
    for item in items:
        if item.get("name") == selector:
            return item
    raise KeyError(f"no list item named {selector!r}")


def _resolve_segment(container: dict[str, Any], segment: str) -> Any:
    key, selector = _parse_segment(segment)
    value = container[key]
    if selector is None:
        return value
    return _select_list_item(value, selector)


def _apply_override(data: dict[str, Any], lever_path: str, value: Any) -> None:
    """Apply one lever override to `data` (a parsed `model.yaml` mapping) in
    place, per the dot-path + `[selector]` scheme."""
    segments = lever_path.split(".")
    container: Any = data
    for segment in segments[:-1]:
        container = _resolve_segment(container, segment)
    final_key, _selector = _parse_segment(segments[-1])
    container[final_key] = value


def _write_variant(
    base_data: dict[str, Any], point: dict[str, Any], tmp_dir: Path, point_id: str
) -> Path:
    """Deep-copy the base model, apply every lever override for `point`, and
    materialize the result as a per-point model.yaml so `load_model`/
    `ReplicationRunner` recompile the variant from scratch, never mutate the
    shared base in place.

    Serializes via `json.dumps` rather than a YAML dumper: JSON is a syntactic
    subset of YAML, so `yaml.safe_load` (the only PyYAML entry point, per
    D-001 and `model/loader.py`) parses it unchanged, and `model/loader.py`
    stays the ONLY importer of PyYAML in the system.
    """
    variant_data = copy.deepcopy(base_data)
    for lever_path, value in point.items():
        _apply_override(variant_data, lever_path, value)

    variant_path = tmp_dir / f"{point_id}.yaml"
    variant_path.write_text(json.dumps(variant_data), encoding="utf-8")
    return variant_path


# ---------------------------------------------------------------------------
# KPI reporting + output partitioning
# ---------------------------------------------------------------------------


def orders_frame(
    plan: list[WorkOrder], compiled: CompiledModel, event_log_path: Path
) -> pl.DataFrame:
    """One (order, lot) row per lot observed at that order's part's terminal
    routing location in this run's own event log — the join key `KpiEngine`
    needs, rebuilt per replication since lot ids are generated fresh (per-
    location, per-firing) every run."""
    events = pl.read_parquet(event_log_path)
    rows: list[tuple[str, str, float, str]] = []
    for order in plan:
        terminal_location = compiled.routing[order.part][-1]
        lot_ids = (
            events.filter(pl.col("location_id") == terminal_location)
            .select("lot_id")
            .unique()
            .to_series()
            .to_list()
        )
        due_date = float(order.due_date)
        for lot_id in lot_ids:
            rows.append((order.work_order_id, order.part, due_date, str(lot_id)))

    if not rows:
        return pl.DataFrame(schema=_ORDERS_SCHEMA)
    return pl.DataFrame(rows, schema=_ORDERS_SCHEMA, orient="row")


def _kpi_rows_for_point(
    point_id: str,
    plan: list[WorkOrder],
    compiled: CompiledModel,
    results: list[RunResult],
) -> list[dict[str, Any]]:
    """One `kpi_table` row per replication: `run_hours`/`on_time_pct` via the
    already-real `KpiEngine`, called once per replication's own event log."""
    rows: list[dict[str, Any]] = []
    for result in results:
        orders = orders_frame(plan, compiled, result.event_log_path)
        kpi_set = KpiEngine().compute(result.event_log_path, orders, result.horizon)
        rows.append(
            {
                "sweep_point": point_id,
                "replication": cast(int, result.run_meta["replication_index"]),
                "run_hours": kpi_set.run_hours,
                "on_time_pct": kpi_set.on_time_pct,
            }
        )
    return rows


def _write_partition(out_dir: Path, point_id: str, results: list[RunResult]) -> None:
    """Write one hive-style `sweep_point=<id>` directory holding every
    replication's event rows, `replication` as an ordinary column — never its
    own directory level."""
    partition_dir = out_dir / f"sweep_point={point_id}"
    partition_dir.mkdir(parents=True, exist_ok=True)

    frames = [
        pl.read_parquet(result.event_log_path).with_columns(
            pl.lit(cast(int, result.run_meta["replication_index"])).alias("replication")
        )
        for result in results
    ]
    combined = pl.concat(frames)
    combined.write_parquet(partition_dir / "events.parquet", compression="zstd")
