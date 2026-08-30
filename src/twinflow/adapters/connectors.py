"""Data connectors — the unified seam between the twin and a system of record.

A real ERP/MES integration is a separate, later job; what ships here is the *surface*
it plugs into, so that job is a single class + one `register(...)` call, never a change
to the core. Three capabilities, each a small protocol:

- **PlanSource** — read a production plan into the canonical `WorkOrder` contract.
- **ResultSink** — write the twin's results (KPIs, a chosen schedule) back out.
- **ActualSource** — read a real production event log for plan-vs-actual reconciliation.

A connector implements whichever it can. The README's promise — "a connector to a system
of record becomes a column mapping onto the existing plan contract, not a rewrite" — is
made concrete by `FieldMapping`: an ERP's own column names are remapped onto twinflow's
canonical plan columns, and the shipped `load_plan` does the rest.

Reference connectors (CSV in, JSON out, parquet actuals) are included so the surface is
usable today; SAP/OPC-UA/Kafka adapters plug in the same way tomorrow.
"""

from __future__ import annotations

import csv
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import polars as pl

from twinflow.modules.registry import Registry
from twinflow.plan.loader import WorkOrder, load_plan
from twinflow.primitives.part import PartTypeRegistry


@dataclass(frozen=True)
class FieldMapping:
    """Rename a source system's columns onto twinflow's canonical plan columns.

    Keys are canonical names (`work_order_id`, `part`, `qty`, `start_date`,
    `due_date`, and the optional initial-WIP columns); values are the column names
    the source actually uses. A canonical column absent from the mapping is assumed
    to already carry its canonical name.
    """

    columns: dict[str, str] = field(default_factory=dict)

    def apply(self, header: list[str]) -> list[str]:
        """Rewrite a source header row into canonical names."""
        source_to_canonical = {source: canonical for canonical, source in self.columns.items()}
        return [source_to_canonical.get(column, column) for column in header]


@runtime_checkable
class PlanSource(Protocol):
    """Reads a production plan from a system of record into `WorkOrder`s."""

    @property
    def name(self) -> str: ...

    def read_plan(self, registry: PartTypeRegistry) -> list[WorkOrder]: ...


@runtime_checkable
class ResultSink(Protocol):
    """Writes the twin's results (KPIs, a chosen scenario/schedule) back out."""

    @property
    def name(self) -> str: ...

    def write_results(self, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class ActualSource(Protocol):
    """Reads a real production event log (for plan-vs-actual reconciliation)."""

    @property
    def name(self) -> str: ...

    def read_actuals(self) -> pl.DataFrame: ...


@dataclass(frozen=True)
class CsvPlanSource:
    """Read a plan CSV whose columns follow a source system's own names, remapped
    onto the canonical plan contract via `FieldMapping`, then validated by the
    shipped `load_plan` (so one loader still owns every fail-closed rule)."""

    path: str
    mapping: FieldMapping = field(default_factory=FieldMapping)
    name: str = "csv_plan"

    def read_plan(self, registry: PartTypeRegistry) -> list[WorkOrder]:
        with Path(self.path).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        if not rows:
            raise ValueError(f"plan file {self.path!r} is empty")
        remapped = [self.mapping.apply(rows[0]), *rows[1:]]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, newline="", encoding="utf-8"
        ) as tmp:
            csv.writer(tmp).writerows(remapped)
            tmp_path = tmp.name
        try:
            return load_plan(tmp_path, registry)
        finally:
            Path(tmp_path).unlink(missing_ok=True)


@dataclass(frozen=True)
class JsonResultSink:
    """Write a results payload (KPIs / a chosen scenario) to a JSON file."""

    path: str
    name: str = "json_result"

    def write_results(self, payload: dict[str, Any]) -> None:
        Path(self.path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class ParquetActualSource:
    """Read a real production event log from a parquet file (same columns the twin's
    own event log uses), so plan-vs-actual reconciliation reads both the same way."""

    path: str
    name: str = "parquet_actual"

    def read_actuals(self) -> pl.DataFrame:
        return pl.read_parquet(self.path)


PLAN_SOURCES: Registry[PlanSource] = Registry("plan_source")
PLAN_SOURCES.register("csv_plan", CsvPlanSource)

RESULT_SINKS: Registry[ResultSink] = Registry("result_sink")
RESULT_SINKS.register("json_result", JsonResultSink)

ACTUAL_SOURCES: Registry[ActualSource] = Registry("actual_source")
ACTUAL_SOURCES.register("parquet_actual", ParquetActualSource)
