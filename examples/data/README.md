# Operational data worked example

Four files backing the operational-data module (RM-05), which stores
normalized operational facts locally, rebuilds what was knowable at a
historical decision time, and scores dated forecasts once outcomes mature.
See
[`docs/enterprise/rm-05/USER-GUIDE.md`](../../docs/enterprise/rm-05/USER-GUIDE.md)
for the full field reference.

## The four files

- **`operational-events.jsonl`** &mdash; six raw source events (order
  creation, MES progress reports, an erroneous stock balance and its later
  correction), one JSON object per line. Each event carries `source`,
  `event_id`, `source_revision`, `occurred_at` (when it happened) and
  `ingested_at` (when it was learned) &mdash; the two separate timestamps
  this module is built around. A later revision of the same
  `(source, event_id)` supersedes an earlier one without deleting it.
- **`freshness-rules.json`** &mdash; two named rules (`erp orders`,
  `MES WIP`), each capping how old a source's latest event may be
  (`maximum_age_seconds`) before a snapshot is marked stale for that rule.
- **`forecasts.json`** &mdash; dated delivery forecasts per order
  (`item_id`), each with a `decision_at` time, a point estimate
  (`point_date`), optional quantile bands, a `segment`, and an optional
  `baseline_date` to compare against.
- **`actuals.json`** &mdash; what really happened per order: an
  `observed_date`, or a `censored_at` time if the outcome isn't known yet.

## Run it

Use a scratch database path, not a file tracked in the repo:

```bash
DB=/tmp/twinflow-operations.db

twinflow data import --db "$DB" examples/data/operational-events.jsonl

twinflow data events --db "$DB" --known-at 2026-01-02T23:59:59Z

twinflow data snapshot --db "$DB" --known-at 2026-01-04T00:00:00Z \
  --freshness-rules examples/data/freshness-rules.json --save

twinflow data validate-forecasts examples/data/forecasts.json examples/data/actuals.json
```

## What each command does

- **`import`** &mdash; loads the JSONL file into the database and reports
  how many events were newly inserted versus already-seen duplicates.
  Re-running the same import is safe: it reports 0 new inserts rather than
  appending copies. Reusing the same `(source, event_id, source_revision)`
  identity with different content fails instead of silently overwriting.
- **`events`** &mdash; returns the retained source records knowable as of
  `--known-at`, i.e. everything with an `ingested_at` at or before that
  time. This is the audit trail, not the reconciled state.
- **`snapshot`** &mdash; reconciles all retained events into current
  entity state as of `--known-at`, and reports both cutoffs, event
  lineage, quality diagnostics, source watermarks, freshness (per rule in
  `--freshness-rules`), and a content-derived `snapshot_id`. `--save`
  persists it so it can be retrieved again later by ID. `ready` is `true`
  only when every configured freshness rule passes.
- **`validate-forecasts`** &mdash; pairs each forecast against its actual
  and reports mean/median absolute error in days, quantile coverage, and
  improvement over the declared baseline, both overall and broken out by
  `segment`. An actual with only `censored_at` counts as censored; a
  missing actual counts as missing. Neither counts toward the error or
  coverage denominator.

## A key distinction: occurred vs. known

The fixture's stock correction **occurred** January 2 but was only
**learned** January 3. Run `snapshot` with `--known-at` set to January 2
and the stock value still reflects the original (wrong) reading; set it to
January 4 (as in the command above) and the correction has landed. This is
the reason the module tracks `occurred_at` and `ingested_at` separately:
`--known-at` always governs what the snapshot can see, and
`--occurred-through` (not used above) is there for when the business event
horizon needs to differ from the knowledge cutoff.
