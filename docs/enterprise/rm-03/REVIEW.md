# RM-03 implementation review

## Result

RM-03 now has a real timezone-aware calendar kernel, explicit shift-crossing behavior,
independent machine instances for capacity-N locations, and a separate resource
evidence ledger. Existing model syntax remains valid and defaults to continuous work.

The implementation follows the existing compile-time/runtime split: immutable calendar
configuration lives on `LaborPoolConfig`, while each run builds a fresh
`WorkingCalendar`, `LaborPool`, and machine set. The ProcessExecution schema remains
unchanged; named resource usage has its own Parquet contract.

## Review findings

- UTC arithmetic drives elapsed simulation time while local timezone conversion drives
  shift membership, handling DST without treating a local day as 24 fixed hours.
- Capacity-N setup decisions now use the acquired machine. The former shared
  `spec.machine.current_setup` race is removed.
- `finish_unattended` releases labor after load and reacquires it for unload. Resource
  evidence records attended seconds separately from occupied machine time.
- KpiEngine divides location busy time by the number of observed machines and exposes
  stable machine IDs rather than attributing all capacity to the location name.
- Calendar and crossing configuration is validated before compilation with located
  errors.

## Validation and limits

Fresh verification on Python 3.13 using the project's pytest, Ruff, and strict mypy
configuration:

- Focused calendar/resource and compatibility suite: 49 passed.
- Full suite: 516 passed.
- Ruff check and format check on all changed Python files: clean.
- mypy: no issues in 65 source files.

The Python patterns typing, error-handling, and tooling references guided the boundary
validation and typed value objects. The tests verify software mechanics against hand
calculations. They do not validate a customer calendar, process model, or promise date.
Unsupported RM-03 breadth is listed explicitly in `COVERAGE.md` and the user guide.
