# RM-09 Dedicated Service Operations Guide

## Local service

Loopback is the safe default and does not require a token:

```bash
python -m twinflow.service.serve --host 127.0.0.1 \
  --workspace-root .twinflow-workspace --models-root examples
```

This mode is for one trusted operator on the same machine. It is not tenant isolation.

## Dedicated network service

Create a caller token registry and set explicit browser/host allowlists before binding beyond loopback:

```bash
export TWINFLOW_TOKENS_FILE=/etc/twinflow/tokens.json
twinflow admin token new --caller planner --scopes read,plan --days 90   # prints the token once
export TWINFLOW_ALLOWED_HOSTS='twinflow.internal.example'
export TWINFLOW_CORS_ORIGINS='https://planner.internal.example'
python -m twinflow.service.serve --host 0.0.0.0 --port 8000 \
  --workspace-root /srv/twinflow/workspace
```

Clients send `Authorization: Bearer <token>` for every `/api` call and for OpenAPI/docs routes.
`/health` and `/ready` are intentionally unauthenticated, return no workspace data, and support
infrastructure probes. Responses include `X-Correlation-ID`; a client may supply up to 128 safe
letters, digits, dots, underscores, or hyphens in that header.

Each token belongs to one named caller with the scopes `read` and/or `plan`; the registry stores only
SHA-256 hashes and is read at startup. Rotate a token by minting a new one, removing the old entry
and restarting the service. Callers are services, not people. This increment does not provide user identities, roles, SSO, or tenant separation. Run
separate service processes and workspace directories for separate trust domains.

Only one process can own a workspace. A second process exits during application construction.
After an unclean restart, queued/running experiments become `interrupted` and cancellation
requests become `canceled`; clients must submit a new request key to retry interrupted work.

## Evidence and bounds

`GET /api/workspace/jobs/{job_id}/evidence?offset=0&limit=100` pages evidence embedded in a job
result. The endpoint never accepts a path. Imports, submissions, cancellations, and exports write
append-only audit metadata in SQLite. Configure runtime bounds by explicitly constructing
`ServiceSettings` when embedding the app; environment construction intentionally exposes only
the bind token and allowlists at present.

## Backup and restore

Stop the service before creating a backup. The command checks the same owner lock as the service
and refuses to run while that workspace is active:

```bash
python -m twinflow.cli.admin backup \
  --workspace /srv/twinflow/workspace --out /backups/twinflow-workspace.zip
```

Restore into a path that is absent or empty:

```bash
python -m twinflow.cli.admin restore /backups/twinflow-workspace.zip \
  --workspace /srv/twinflow/restored-workspace
```

The archive contains `workspace.sqlite3`, optional `operational-data.sqlite` and `actions.sqlite3`
when present, fixed-root artifacts, and SHA-256 metadata. Each database is copied through SQLite's
online backup API while the workspace is quiesced. The output path must be outside the workspace.
Restore streams data into staging and validates member paths, the exact allowlisted manifest,
metadata types, sizes, digests, and every SQLite database before installing the staged workspace.
Run a periodic restore drill; creating an archive alone does not prove recoverability. This
facility is a local checkpoint, not continuous replication or point-in-time recovery.
