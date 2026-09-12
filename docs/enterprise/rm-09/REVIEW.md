# RM-09 Engineering Review

## Result

The service now has an honest dedicated-workspace deployment boundary. Loopback remains easy for
local use. Network exposure fails closed without a configured token, host allowlist, and browser
origin allowlist.

## Findings

- Service settings are frozen and injected. The process runner reads environment once and rejects
  a non-loopback bind without `TWINFLOW_API_TOKEN`; direct nonlocal settings reject the same state.
  Bearer checks use constant-time comparison and
  cover API, schema, OpenAPI, and documentation routes. Public probes disclose only status.
- Trusted hosts, restrictive CORS, bounded bodies, correlation IDs, and sanitized unexpected
  errors are enforced in middleware. Negative lengths fail and chunked bodies are bounded before
  dispatch. A post-start failure is never converted into a second HTTP response start. Pydantic
  request models retain their narrower field bounds.
- An exclusive OS file lock prevents two processes from recovering or executing the same
  workspace. SQLite uses WAL and immediate transactions for idempotency plus queue admission.
  Matching replay wins before the full-queue check.
- Compare-and-set job transitions prevent late success/failure from replacing cancellation or a
  terminal result. Startup resolves queued/running/cancel_requested states explicitly.
- Evidence lookup uses a server-resolved job and bounded page parameters. Server-generated job IDs
  remain the only artifact-directory selectors. Audit metadata is append-only for import,
  evaluate, cancel, and export operations.
- Backup requires the owner lock to be free and uses SQLite's online backup API separately for
  `workspace.sqlite3` and optional `operational-data.sqlite`/`actions.sqlite3`. Output-inside-source,
  database/artifact symlinks, and unknown files are refused. Restore streams members and checks
  typed allowlisted metadata, path safety, duplicate/unlisted files, total size/count, SHA-256
  digests, required tables, and every SQLite quick-check before an atomic directory rename.

## Remaining limits

Authentication is one shared bearer secret. There is no federated identity, role/project model,
PostgreSQL row security, tenant boundary, isolated worker service, encryption/key-manager
integration, retention/deletion workflow, remote artifact store, telemetry backend, signed
release, rolling upgrade, or measured SLO. The legacy `/api/run`, `/api/sweep`, and `/api/optimize`
compatibility endpoints still use their original in-process job store; durable idempotent jobs are
the `/api/workspace` application contract. A backup captures SQLite consistently, but operators
must quiesce artifact producers when they require a single artifact checkpoint.

These limits mean the broader RM-09 roadmap and procurement exit gate remain open. The increment
is suitable for a bounded dedicated pilot after normal deployment review, not a shared multi-tenant
  service claim.

## Verification scope

Focused tests cover auth denial and success, docs/schema protection, probe access, restrictive
CORS and hosts, declared/chunked body bounds, post-start failure behavior, direct/runner remote-mode
refusal, idempotent replay under a full queue, terminal
compare-and-set behavior, restart recovery, owner locking, backup/restore, traversal rejection,
and the admin CLI. Backup adversarial cases cover active ownership, all allowlisted databases,
inside-source output, missing sources, database symlinks, streamed restore, and malformed manifest
metadata. Fresh lint, strict typing, focused tests, and the full suite are reported with
the commit.
