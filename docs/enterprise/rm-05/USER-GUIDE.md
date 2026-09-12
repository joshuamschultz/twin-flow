# RM-05 User Guide

The RM-05 module stores normalized operational facts locally, rebuilds what was knowable at a
historical decision time, and measures dated forecasts after outcomes mature. It uses only the
Python standard library and writes to a SQLite database you choose.

## Try the complete example

From the repository root:

```bash
export PYTHONPATH="$PWD/src"
python -m twinflow.cli.data import --db /tmp/twinflow-operations.db \
  examples/data/operational-events.jsonl
python -m twinflow.cli.data events --db /tmp/twinflow-operations.db \
  --known-at 2026-01-02T23:59:59Z
python -m twinflow.cli.data snapshot --db /tmp/twinflow-operations.db \
  --known-at 2026-01-04T00:00:00Z \
  --freshness-rules examples/data/freshness-rules.json --save
python -m twinflow.cli.data validate-forecasts \
  examples/data/forecasts.json examples/data/actuals.json
```

The `events` command returns retained source records. The snapshot command prints reconciled
entity state, both cutoffs, event lineage, quality diagnostics, source watermarks, freshness,
and a content-derived snapshot ID. Repeating an import reports duplicates; it does not append
copies. Reusing the same source/event/revision identity with different content fails.

Use `--occurred-through` when the business event horizon differs from the knowledge cutoff.
The known-at cutoff always governs ingestion time. For example, the fixture's stock correction
occurred January 2 but was learned January 3, so a January 2 snapshot retains the original
quantity and a January 4 snapshot applies the correction.

## Python API

```python
from datetime import UTC, datetime
from twinflow.data import FreshnessRule, SQLiteEventStore, SnapshotBuilder, load_events

store = SQLiteEventStore("operations.db")
result = store.ingest(load_events("events.jsonl"))
builder = SnapshotBuilder(
    store,
    freshness_rules=[FreshnessRule("MES WIP", 3600, source="mes", entity_type="operation")],
)
snapshot = builder.as_known_at(datetime(2026, 1, 4, tzinfo=UTC), save=True)
```

`EventStore` is the storage protocol for another repository implementation. `raw_record()` on
the SQLite implementation retrieves canonical source lineage by `(source, event_id,
source_revision)`. `snapshot_document()` retrieves an immutable saved snapshot JSON document.

For validation, construct `ForecastRecord` and `ActualRecord` values and call
`score_forecasts()`. An actual with only `censored_at` is counted as censored. An absent actual
or an actual with neither outcome field is counted as missing. Neither contributes to an error
or coverage denominator. Baseline improvement is `(baseline MAE - forecast MAE) / baseline
MAE` on the paired subset and is `null` when no usable baseline exists or baseline MAE is zero.

## Operational limits

The generic reducer merges event payload fields and recognizes `entity_deleted`. A real source
connector must define source ownership, references, units, and event mapping before use. This
increment does not fit models, select holdouts, estimate grouped uncertainty, approve decision
models, monitor drift, or establish customer acceptance. A `ready` snapshot only means its
configured local quality and freshness checks passed.
