# HMLV calendar and independent machines

This runnable example declares a timezone-aware weekday labor calendar, one closed
date, paused work at shift end, and a two-machine center. Run it with:

```bash
twinflow run examples/hmlv-calendar/model.yaml \
  --plan examples/hmlv-calendar/plan.csv --reps 1
```

The run's `resource_usage.parquet` names `mill-1` and `mill-2`. Both start cold and
therefore pay their own 600-second setup before processing in parallel.
