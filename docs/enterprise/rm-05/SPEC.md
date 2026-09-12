# RM-05 Operational Data, Snapshots, and Forecast Validation

## Scope

This increment provides a local, read-only operational data boundary. It accepts bounded
normalized JSON or JSONL events, retains the original record, deduplicates on
`(source, event_id, source_revision)`, creates deterministic snapshots using both event-time
and ingestion-time cutoffs, reports data quality and freshness, and scores dated forecasts
against matured actuals and optional baselines.

It does not claim a production partner connector, parameter fitting, model approval, drift
monitoring, or customer acceptance. Those require customer-specific source ownership and
evidence.

## Event contract

Every event has `source`, `event_id`, `source_revision`, `occurred_at`, `ingested_at`,
`entity_type`, `entity_id`, `event_type`, `payload`, and optional `supersedes_event_id`.
Timestamps must be timezone-aware ISO-8601 values. Identifiers and payload size/depth are
bounded at ingestion. The exact parsed source object is retained as canonical JSON together
with its digest. Re-importing an identical identity and body is an idempotent no-op; using the
same identity for different content is a conflict.

Corrections are explicit. A correction names the prior event ID in the same source. At a
cutoff it suppresses every known revision of that event, then participates as a normal event.
For each remaining event identity, the greatest natural revision is active. Reordering input
does not alter the result.

## Snapshot contract

`SnapshotBuilder.as_known_at(known_at, occurred_through=None)` only reads records whose
`ingested_at <= known_at` and `occurred_at <= occurred_through` (default: `known_at`). Active
events are ordered by occurred time, ingestion time, source, event ID, and natural revision.
Payload fields are merged into entity state. A payload `null` removes a field; an event type
`entity_deleted` removes the entity. The immutable snapshot contains a content-derived ID,
both cutoffs, source watermarks, entity states, contributing event identities, diagnostics,
and freshness results. Saving the same snapshot twice is idempotent; a snapshot ID cannot be
overwritten with different content.

Quality diagnostics include delayed events, unresolved corrections, future occurrence,
revision gaps, and empty payloads. Freshness rules are per source or entity type and report
fresh, stale, or missing dependencies. Snapshot readiness is false for stale or missing rules
or error-severity diagnostics.

## Forecast validation contract

A forecast names an item, decision time, point date, optional dated quantiles, segment, and
optional baseline date. An actual names an item and either an observed date, a censoring time,
or neither. Scoring uses only actuals matched by item. Missing and censored outcomes stay
explicit and are excluded from date-error and coverage denominators. The report includes
matched, scored, missing, and censored counts; mean and median absolute date error; empirical
quantile coverage and count; and baseline error plus relative improvement on rows with both
forecast and baseline. No model fitting or statistical independence claim is made.

## CLI and storage

`python -m twinflow.cli.data` supports `import`, `events`, `snapshot`, and
`validate-forecasts`. SQLite access is parameterized and wrapped in context managers. The
repository protocol keeps storage replaceable. Commands emit JSON suitable for application
adapters; the parent integration may attach these parsers to the primary CLI.
