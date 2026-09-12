"""ScoringSurface — the one seam every module attaches to.

A `Scenario` is a set of lever overrides (a point in `LeverSpace`). `evaluate` turns
it into an `Evaluation` by running the twin: it writes a variant `model.yaml`, runs
`reps` replications at a fixed `base_seed` (so replication i of every scenario shares
the same random draws — common random numbers, D-033), and reports the KPIs of the
representative replication plus the confidence intervals across all of them.

Objectives, cost functions, optimizers, and surrogate models never run the simulator
themselves; they consume `Evaluation`s. That keeps the "twin scores, the module
decides" boundary the README draws: the surface produces honest, uncertainty-carrying
numbers, and a module turns them into a single score or a proposed change.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.aggregate import AggregatedKpis, aggregate_kpis
from twinflow.instrumentation.inventory import InventoryKpis, compute_inventory_kpis
from twinflow.instrumentation.kpis import KpiSet
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import CompiledModel, load_model
from twinflow.model.loader import load_raw_model
from twinflow.plan.loader import WorkOrder
from twinflow.plan.replication import ReplicationRunner

# Mirrors SweepHarness's committed lever-path scheme: a dot-path segment is a bare
# key optionally followed by `[selector]` picking one item (by index or by `name`)
# from the list that key resolves to.
_SEGMENT_RE = re.compile(r"^([^\[\]]+)(?:\[(.+)\])?$")


@dataclass(frozen=True)
class Scenario:
    """A named point in lever space: dot-path lever -> assigned value."""

    levers: dict[str, Any]
    label: str = ""

    def describe(self) -> str:
        """A stable one-line identity, e.g. `labor.pools[0].headcount=3, ...`."""
        if self.label:
            return self.label
        return ", ".join(f"{key}={value}" for key, value in sorted(self.levers.items()))


@dataclass(frozen=True)
class Evaluation:
    """The scored outcome of running one `Scenario`.

    `kpis` is the representative (first) replication; `intervals` is the
    uncertainty band across every replication. `compiled` is the variant's
    compiled model, so a cost function can read the scenario's actual headcount
    or capacity without re-parsing.
    """

    scenario: Scenario
    kpis: KpiSet
    intervals: AggregatedKpis
    compiled: CompiledModel = field(repr=False)
    horizon: float = 0.0
    """The representative replication's simulated horizon (seconds) — the paid
    time base for inventory holding and other rate costs."""

    inventory: InventoryKpis = field(default_factory=lambda: InventoryKpis({}, {}, {}, {}, {}))
    """Per-stock supply-chain KPIs (empty for a model with no stocks)."""
    per_replication_kpis: tuple[KpiSet, ...] = ()
    per_replication_inventory: tuple[InventoryKpis, ...] = ()
    artifact_dir: Path | None = None


class ScoringSurface:
    """Turns a `Scenario` into an `Evaluation`. Constructed once against a base
    model + plan, then called many times (by an optimizer, an agent, or the CLI).

    Reuses the shipped run stack unchanged: `ReplicationRunner` for CRN-correct
    parallel replications and `KpiEngine` for every KPI. Each `evaluate` writes
    its variant to a throwaway file and runs from a scratch working directory, so
    a thousand-evaluation search never litters the caller's tree with `runs/`.
    """

    def __init__(
        self,
        model_path: str,
        plan: list[WorkOrder],
        reps: int = 20,
        base_seed: int = 0,
        artifact_dir: str | Path = "artifacts/evaluations",
        max_workers: int = 4,
        max_sim_time: float | None = None,
        max_events: int | None = None,
        max_wall_seconds: float | None = None,
    ) -> None:
        if reps < 1:
            raise ValueError("reps must be a positive integer")
        self._model_path = model_path
        self._plan = plan
        self._reps = reps
        self._base_seed = base_seed
        self._artifact_dir = Path(artifact_dir)
        self._max_workers = max_workers
        self._max_sim_time = max_sim_time
        self._max_events = max_events
        self._max_wall_seconds = max_wall_seconds
        self._base_data = cast(dict[str, Any], load_raw_model(model_path).data)

    @property
    def reps(self) -> int:
        return self._reps

    def evaluate(self, scenario: Scenario) -> Evaluation:
        """Run `scenario` and return its `Evaluation` (representative KPIs + CI band)."""
        evaluation_dir = self._artifact_dir / f"evaluation-{uuid.uuid4().hex}"
        evaluation_dir.mkdir(parents=True, exist_ok=False)
        variant_path = self._write_variant(scenario, evaluation_dir)
        compiled = load_model(str(variant_path))
        results = sorted(
            ReplicationRunner(str(variant_path)).run(
                self._plan,
                self._reps,
                self._base_seed,
                artifact_dir=evaluation_dir / "runs",
                max_workers=self._max_workers,
                max_sim_time=self._max_sim_time,
                max_events=self._max_events,
                max_wall_seconds=self._max_wall_seconds,
            ),
            key=lambda result: cast(int, result.run_meta["replication_index"]),
        )
        per_rep = [
            compute_kpis(
                result.event_log_path,
                orders_frame(self._plan, compiled, result.event_log_path),
                result.horizon,
            )
            for result in results
        ]
        per_inventory = [
            compute_inventory_kpis(
                result.event_log_path.parent / "inventory.parquet", result.horizon
            )
            for result in results
        ]
        return Evaluation(
            scenario=scenario,
            kpis=per_rep[0],
            intervals=aggregate_kpis(per_rep),
            compiled=compiled,
            horizon=results[0].horizon,
            inventory=per_inventory[0],
            per_replication_kpis=tuple(per_rep),
            per_replication_inventory=tuple(per_inventory),
            artifact_dir=evaluation_dir,
        )

    def _write_variant(self, scenario: Scenario, tmp_dir: Path) -> Path:
        """Deep-copy the base model, apply the scenario's lever overrides, and
        materialise it as JSON (a YAML subset, so `model/loader.py` stays the
        only PyYAML entry point — same trick `SweepHarness` uses)."""
        variant_data = copy.deepcopy(self._base_data)
        for lever_path, value in scenario.levers.items():
            _apply_override(variant_data, lever_path, value)
        variant_path = tmp_dir / "variant.yaml"
        variant_path.write_text(json.dumps(variant_data), encoding="utf-8")
        return variant_path


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
    """Apply one lever override in place, per the dot-path + `[selector]` scheme."""
    segments = lever_path.split(".")
    container: Any = data
    for segment in segments[:-1]:
        container = _resolve_segment(container, segment)
    final_key, selector = _parse_segment(segments[-1])
    if selector is None:
        container[final_key] = value
    else:
        _assign_list_item(container[final_key], selector, value)


def _assign_list_item(items: list[Any], selector: str, value: Any) -> None:
    if selector.isdigit():
        items[int(selector)] = value
        return
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("name") == selector:
            items[index] = value
            return
    raise KeyError(f"no list item named {selector!r}")
