# RM-05 Engineering Review

## Result

The self-contained operational data increment implements the local foundations of RM-05. It
keeps raw facts separate from reconciled snapshots and forecast scores, applies ingestion-time
knowledge boundaries, and leaves customer-specific integration and statistical claims open.

## Review findings

- Event identities are immutable. Exact replays are no-ops and conflicting bodies fail. SQLite
  statements are parameterized and resources use context managers.
- Snapshot output is stable across input ordering. Natural revision selection happens before
  explicit correction suppression. Both `occurred_at` and `ingested_at` are normalized to UTC,
  so equivalent source offsets have the same cutoff behavior.
- Late corrections do not enter an earlier known-at snapshot. Raw canonical JSON and its digest
  remain queryable, while active normalized event identities are retained in every snapshot.
- Unknown correction targets and future-occurrence records block readiness. Delays, revision
  gaps, and empty payloads remain visible warnings. Per-input freshness rules preserve missing
  and stale states.
- Forecast metrics exclude censored and missing outcomes from denominators and report their
  counts. Quantile coverage includes both numerator and count. Baseline improvement uses only
  paired mature rows.

## Residual risks and deferred roadmap scope

No production ERP/MES connector or partner mapping is included, so the roadmap's partner
integration and acceptance gate remain unmet. Generic payload merge cannot perform domain
reconciliation such as inventory transaction summation, genealogy, or cross-system reference
resolution without an agreed source mapping. Forecast scoring is descriptive: it does not
implement rolling-origin orchestration, fit/model-selection/final period enforcement, grouped
uncertainty, quantile loss, probability calibration, service outcomes, approval registries, or
drift monitoring. SQLite is appropriate for local use; a hosted multi-tenant service still
needs authenticated source scope and transactional PostgreSQL/object-store boundaries.

## Verification

The dedicated suite covers idempotency/conflict behavior, raw lineage, late corrections,
revision and reorder invariance, event-time cutoff, source timezone normalization, freshness,
censoring/missing outcomes, date errors, interval coverage, baseline improvement, and CLI
round trips. Fresh Ruff, strict mypy, dedicated pytest, and full regression results are recorded
in the delivery report.
