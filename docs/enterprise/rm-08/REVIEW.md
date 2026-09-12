# RM-08 Engineering Review

## Result

The increment provides an executable, registry-ready supply-network domain with no industry
branches. Manufacturing assembly and distribution-only examples use the same validation and
evaluation code.

## Findings

- BOM validation checks references, component units, positive multiplicity, revision date ranges,
  overlapping effective revisions, and cycles. Evaluation selects the single revision effective
  at the snapshot time and recursively multiplies component demand.
- Allocation starts from existing reservations and uses a shared remaining-quantity ledger.
  Overallocated or quality-held reservations fail validation; evaluation never lets one stock lot
  or receipt satisfy incompatible promises twice.
- Quality holds stay unavailable. Assembly builds require a process with a supplier qualification
  valid when components become ready. Evidence gates match item, evidence type, validity start,
  and expiry at the forecast date; results retain rule revisions and evidence IDs.
- Supplier delay assumptions are explicitly configured. A single seeded draw per common risk group
  affects every exposed receipt in a replication. Repeated seeds reproduce aggregate dates.
- Incomplete results preserve shortages, dependency paths, blocked gates, and affected orders.
  Result envelopes and evidence artifacts are JSON safe for an API or UI.

## Deferred scope and risk

This is material-feasibility forecasting, not resource-constrained supplier or factory scheduling.
It does not model supplier capacity buckets, transport calendars/cutoffs, MOQ/lot multiples, cost,
substitutions, split-shipment optimization, genealogy, rework, partner visibility enforcement, or
mitigation optimization. Qualifications and document gates evaluate configured records only.
There is no production partner history, forecast-accuracy evidence, access-control proof, or legal
compliance claim. Those roadmap exit-gate items remain open.

The local evaluator intentionally uses indexed dictionaries and recursive edges rather than a
graph database. A transactional hosted allocation service would still need concurrency control,
tenant scope, durable snapshot identity, and PostgreSQL constraints.

## Verification coverage

Regression tests cover the multi-tier happy path and shortage, quantity over-allocation, expired
evidence, missing process qualification, quality hold, correlated supplier disruption,
deterministic seeds, malformed BOM cycles, overlapping effective revisions, bounded replications,
artifact creation, the adapter seam, and the distribution-only profile. Fresh Ruff, strict mypy,
focused pytest, and full regression evidence are reported with the commit.
