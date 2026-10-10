# Twinflow Dot hosted pilot boundary

This is the design gate for a hosted pilot, not a statement that the current local service is ready for customer data. The existing shared `TWINFLOW_API_TOKEN` authenticates a trusted service caller, not a person or organization.

## Identity and authorization

The public HTTPS `/mcp` gateway must implement OAuth 2.1 for each end user. It validates issuer, audience, signature, expiration, scopes, and token revocation policy on every request. The gateway resolves a verified `(user, organization, project, workspace)` tuple, checks membership and role for every tool, and passes an internal signed principal/context to a dedicated Twinflow workspace service. The Twinflow service token remains server-side and never appears in ChatGPT tool output or browser state. Client-provided IDs, paths, or scenario provenance cannot select an unauthorized workspace.

Authorization must cover list/read/branch/run/compare/evidence/export for scenarios and jobs, typed drafts, uploaded files and schedule snapshots, schedule plans, backups, and deletion. Cross-tenant denial tests must cover guessed IDs and pagination as well as list routes. Audit records should bind the end-user principal, organization, request ID, job ID, scenario digest, and authorization outcome.

## Pilot storage and workers

Use one isolated filesystem/database workspace and one process owner per customer. Route a verified tenant to its own owner; never share one writable workspace across replicas. The current file lock and in-process executor are a local safety measure, not a durable distributed queue. Before horizontal scaling, introduce explicit storage ownership, a durable queue, idempotent submission, recoverable job states, quotas, cancellation, backups, restore tests, and retention/deletion jobs. Original schedule upload bytes are currently discarded immediately; committed normalized rows remain in the workspace until a deletion policy is implemented.

## Exposure gate

Before customer files or public app distribution: deploy HTTPS MCP, exercise OAuth and cross-tenant denial tests, set request/compute quotas and cost limits, verify backup/restore and deletion, add health/readiness and monitoring, document privacy/data handling, and provide reviewed test credentials and tool descriptions for app review. Keep all synthetic model results labeled provisional until calibration, held-out forecast tests, freshness checks, and drift monitoring support stronger claims. Secure MCP Tunnel is suitable only for private developer testing.
