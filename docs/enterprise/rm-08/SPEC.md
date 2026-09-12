# RM-08 Industry-neutral Supply Network Specification

## Scope and seam

The `twinflow.domain.supply_chain` package accepts JSON-shaped model and snapshot mappings.
It independently validates and evaluates them through `validate(model, snapshot)`,
`describe(model, snapshot)`, and `evaluate(model, snapshot, seed, artifact_dir, limits)`.
`SupplyChainDomain` mirrors that contract for registration by the shared domain registry.

This increment forecasts material feasibility and delivery dates. It does not schedule factory
resources, interpret law, certify compliance, optimize cost, or claim validation against partner
history. Configured gates are data rules owned by domain users.

## Input contract

Models declare sites, suppliers, items, effective BOM revisions, processes, supplier-process
qualifications, evidence gates, and optional supplier delay risks. Snapshots declare an aware
`as_of` time, inventory lots, expected receipts, existing allocations, evidence documents, and
orders. Unknown fields are retained by the caller but ignored by this version. Required fields,
IDs, positive quantities, units, references, date order, BOM cycles, overlapping BOM revisions,
and duplicate IDs are validated before execution.

A BOM line's unit must equal the referenced component item's unit. An order unit must equal its
item unit. Exactly one BOM revision may be effective for an assembly at the snapshot date.
Explosion multiplies quantities through every tier and rejects cycles before evaluation.

## Allocation and feasibility

Supply is a physical inventory lot or expected receipt. Existing allocations reserve supply by
ID before forecast allocation; total reservations cannot exceed the supply quantity. A reserved
supply must be released and at the order's site. Reservations are pegged by order and item, so a
component reservation is consumed when that order's recursive BOM reaches the component. New orders
are processed by priority, due date, then ID. Each physical supply quantity is consumed at most
once across all orders and component requirements.

Purchased items use available inventory and receipts. Assemblies consume finished assembly
supply first, then explode unmet quantity through the effective BOM. A build requires its
configured process, at least one supplier qualification valid on the build date, and all
configured gates. Quality-held supply is unavailable. Evidence must match gate type and subject,
be valid by `as_of`, and remain unexpired at the evaluated date. Gate outcomes name reasons,
evidence references, and affected orders; they make no legal-compliance claim.

The forecast date is the latest component availability plus configured process lead time.
Incomplete material, missing BOMs, failed qualifications, and blocked gates remain explicit.
Shortages name item, unit, missing quantity, order, and dependency path. Result explanations
trace each blocking dependency to affected orders.

## Uncertainty and results

Supplier risks use a configured uniform delay range and optional `common_risk_group`. One latent
uniform quantile per group per replication is mapped through each supplier's own minimum/maximum
range, preserving correlation without replacing unequal supplier distributions.
`limits.replications` is bounded to 1–1000, `max_events` to 1–1,000,000, and
`max_wall_seconds` to 300. Recursive explosion, allocation, gates, supplies, orders, and
replications cooperatively consume the budget. Exhaustion returns `limit_reached`, the reason,
completed replication count, and work count.

The JSON-safe result contains schema version, domain, status, seed, requested/completed replication
counts, order forecasts, shortages, gate results, affected-order explanations, aggregate metrics,
assumptions, and optional evidence artifact references. Every completed replication sample is
retained. Aggregates use all samples: infeasible dates are explicitly censored, quantile counts
name their denominator, and shortage/gate output is the union with occurrence counts and sample
indices. Artifact writes are atomic. No graph database is used.
