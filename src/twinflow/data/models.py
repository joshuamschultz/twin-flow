"""Value objects for normalized operations, snapshots, and validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Literal

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
DiagnosticSeverity = Literal["warning", "error"]
FreshnessStatus = Literal["fresh", "stale", "missing"]


@dataclass(frozen=True)
class OperationalEvent:
    source: str
    event_id: str
    source_revision: str
    occurred_at: datetime
    ingested_at: datetime
    entity_type: str
    entity_id: str
    event_type: str
    payload: Mapping[str, JsonValue]
    supersedes_event_id: str | None = None
    raw_record: str = ""
    raw_digest: str = ""

    @property
    def identity(self) -> tuple[str, str, str]:
        return (self.source, self.event_id, self.source_revision)


@dataclass(frozen=True)
class IngestResult:
    inserted: int
    duplicates: int


@dataclass(frozen=True)
class DataDiagnostic:
    code: str
    severity: DiagnosticSeverity
    message: str
    event_identity: tuple[str, str, str] | None = None


@dataclass(frozen=True)
class DataQualityReport:
    event_count: int
    diagnostics: tuple[DataDiagnostic, ...]

    @property
    def error_count(self) -> int:
        return sum(item.severity == "error" for item in self.diagnostics)


@dataclass(frozen=True)
class FreshnessRule:
    name: str
    maximum_age_seconds: float
    source: str | None = None
    entity_type: str | None = None


@dataclass(frozen=True)
class FreshnessItem:
    name: str
    status: FreshnessStatus
    latest_occurred_at: datetime | None
    age_seconds: float | None


@dataclass(frozen=True)
class FreshnessAssessment:
    as_of: datetime
    items: tuple[FreshnessItem, ...]

    @property
    def ready(self) -> bool:
        return all(item.status == "fresh" for item in self.items)


@dataclass(frozen=True)
class EntityState:
    entity_type: str
    entity_id: str
    values: Mapping[str, JsonValue]


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    known_at: datetime
    occurred_through: datetime
    source_watermarks: Mapping[str, datetime]
    entities: tuple[EntityState, ...]
    event_identities: tuple[tuple[str, str, str], ...]
    quality: DataQualityReport
    freshness: FreshnessAssessment

    @property
    def ready(self) -> bool:
        return self.quality.error_count == 0 and self.freshness.ready


@dataclass(frozen=True)
class ForecastRecord:
    item_id: str
    decision_at: datetime
    point_date: datetime
    quantiles: Mapping[float, datetime] = field(default_factory=dict)
    segment: str = "all"
    baseline_date: datetime | None = None


@dataclass(frozen=True)
class ActualRecord:
    item_id: str
    observed_date: datetime | None = None
    censored_at: datetime | None = None


@dataclass(frozen=True)
class QuantileCoverage:
    quantile: float
    covered: int
    count: int

    @property
    def empirical_coverage(self) -> float | None:
        return self.covered / self.count if self.count else None


@dataclass(frozen=True)
class BacktestReport:
    forecast_count: int
    matched_count: int
    scored_count: int
    missing_count: int
    censored_count: int
    mean_absolute_error_days: float | None
    median_absolute_error_days: float | None
    quantile_coverage: tuple[QuantileCoverage, ...]
    baseline_count: int
    baseline_mean_absolute_error_days: float | None
    baseline_improvement_fraction: float | None
    segments: Mapping[str, BacktestReport] = field(default_factory=dict)


def immutable_mapping(values: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """Return a read-only shallow copy for frozen public value objects."""
    return MappingProxyType(dict(values))
