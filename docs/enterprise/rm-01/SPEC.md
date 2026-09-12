# RM-01: trustworthy result foundations

## Requirements

- RM01-R1: Completion is earned only when accepted output from the terminal routing
  operation satisfies an order's required quantity. Intermediate events and scrap do
  not complete an order.
- RM01-R2: Every planned order has an explicit quantity ledger and outcome. A bounded
  run reports `completed`, `incomplete_at_horizon`, `infeasible`, or `failed`, with
  remaining demand and any completion time.
- RM01-R3: A caller may bound simulated time, processed events, and wall time. The
  result names the termination reason; natural event exhaustion is distinct from
  demand completion.
- RM01-R4: Replication statistics distinguish observed outcome quantiles from a
  Student-t confidence interval on an estimated mean. Censored completions are counted
  and their quantiles are not estimated from the completed subset.
- RM01-R5: An evaluation retains all per-replication KPI and inventory results, and
  objectives use the requested replicated statistic rather than the first trace.
- RM01-R6: Drivers and replication workers accept explicit artifact directories,
  never change process-wide working directory, and use a bounded worker count.
- RM01-R7: Retained evidence includes the resolved model, plan, limits, seed scheme,
  runtime/dependency identity, and result metadata in the artifact tree.

## Component boundaries and public contracts

- `plan.driver`: owns run budgets, terminal fulfillment accounting, termination state,
  and per-run evidence. `RunDriver.run` keeps its positional arguments and adds
  keyword-only `artifact_dir`, `max_sim_time`, `max_events`, and `max_wall_seconds`.
- `plan.replication`: owns bounded process dispatch and isolated replication paths.
  `ReplicationRunner.run` adds keyword-only artifact and budget arguments.
- `plan.driver`: observes order-aware terminal sinks and retains accepted/scrapped
  quantities in the run evidence ledger while preserving the event-log schema.
- `instrumentation.kpis`: derives complete order accounting from planned demand plus
  terminal event evidence.
- `instrumentation.aggregate`: exposes outcome quantiles, a mean confidence interval,
  sample count, censoring count, and estimability separately.
- `modules.surface` and `modules.objectives`: retain all replication metrics and score
  their aggregate distributions. The first trace remains a visualization convenience.

## Tasks

1. Add terminal quantity evidence and order outcome value objects.
2. Add deterministic bounded stepping and explicit termination reasons.
3. Thread artifact roots and budgets through replication and scoring.
4. Replace ambiguous statistical naming while retaining compatibility accessors.
5. Retain per-replication evaluation data and evidence inputs.
6. Add analytical, censoring, isolation, and regression tests.
7. Run focused and full pytest, Ruff, and mypy; review and document limits.

## Acceptance tests

- An event at a non-terminal operation cannot complete an order.
- Scrapped terminal quantity cannot complete an order; multiple accepted terminal lots
  can satisfy one order exactly.
- Every order appears in the ledger, including unreleased and unfinished orders.
- A simulated-time or event limit returns `incomplete_at_horizon` with remaining demand.
- A completion distribution containing censored replications reports the censored count
  and `quantiles=None`; a complete synthetic sample matches independently calculated
  quantiles and mean CI.
- Concurrent replications write under distinct caller-provided directories and leave
  the process working directory unchanged.
- Worker count never exceeds the configured bound.
- Evidence files permit inspection of the exact resolved model, plan, seed, limits,
  package version, Python version, and dependency fingerprint used.

These tests establish software semantics and reproducibility evidence. They do not by
themselves validate a model against a real operation or support promise dates.
