# RM-09 Dedicated Service Hardening Specification

## Deployment boundary

This increment supports one dedicated workspace owned by one service process. Local mode binds
to loopback and may run without authentication. A non-loopback bind is refused unless
`TWINFLOW_API_TOKEN` is configured. Token mode protects API, data, schema, and interactive API
documentation routes with constant-time Bearer comparison. It is a shared service token, not
federated identity or multitenancy.

`ServiceSettings` is constructed at the process boundary and injected into `create_app` and
`Workspace`. CORS origins and allowed hosts are explicit allowlists. Request bodies, active jobs,
stored records, and evidence results have configured bounds. Public `/health` and `/ready`
endpoints reveal only process/readiness status. Every response has a validated or generated
correlation ID. Unexpected responses are sanitized while detailed failures remain in server logs.
Constructing settings with `local_only=False` and no token fails immediately, including direct
application embedding that does not use the process runner. Negative declared body lengths fail;
chunked requests are bounded before application dispatch. A downstream failure after response
start is logged and propagated without attempting a second response start.

## Durable jobs and ownership

A workspace holds an exclusive nonblocking process lock until close. A second server cannot own
the same repository concurrently. Job idempotency and active-queue admission occur in one SQLite
`BEGIN IMMEDIATE` transaction; an existing matching idempotency key is returned even when the
queue is full. Reuse with another digest fails.

Job changes use compare-and-set transitions. Valid flow is queued to running or cancel_requested;
running to cancel_requested; running to completed/failed; and cancel_requested to canceled.
Terminal states cannot be overwritten by a late worker or cancellation. On startup, queued and
running jobs become interrupted; cancel_requested jobs become canceled.

Import, experiment submission, cancellation, and exports append bounded audit events. Audit data
records operation, object kind/ID, outcome, time, and correlation metadata supplied by the trusted
service boundary. It does not claim user identity.

## Evidence and artifacts

Evidence queries accept a job ID and bounded offset/limit only. The repository returns evidence
embedded in that job result; callers cannot submit artifact paths. Artifact creation remains
under `<workspace>/artifacts/<server-generated-job-id>`.

## Backup and restore

The standalone admin CLI exposes `backup --workspace ROOT --out ARCHIVE` and
`restore ARCHIVE --workspace ROOT`. Backup uses SQLite's online backup API, includes regular
artifact files beneath the fixed artifact root, and includes the required `workspace.sqlite3`
plus optional allowlisted `operational-data.sqlite` and `actions.sqlite3` through separate online
backup calls. The output must be outside the source workspace. Database and artifact symlinks are
refused. Backup acquires the workspace owner lock nonblockingly and refuses an active service,
giving all databases and artifacts a quiesced checkpoint boundary. Sorted ZIP members carry a
bounded SHA-256 manifest. Restore streams members and rejects absolute/traversing/unlisted/
duplicate members, malformed or oversized metadata, size/digest mismatches, invalid SQLite
databases, symlinks, and nonempty targets. Extraction occurs in a sibling staging directory before
atomic rename.

The service must be stopped before backup. This local checkpoint does not provide continuous
point-in-time recovery or remote replication.
