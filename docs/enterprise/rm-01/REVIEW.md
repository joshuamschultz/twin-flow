# RM-01 implementation review

## Result

The implementation establishes meaningful foundations for trustworthy results:
terminal accepted-quantity accounting, explicit incomplete outcomes, three execution
bounds, bounded replication workers, isolated artifact roots, retained run evidence,
separate outcome quantiles and mean confidence intervals, censoring disclosure, and
replicated objective inputs.

The prior order KPI path inferred completion from the maximum release time of any lot
assigned to an order. For same-part orders, the adapter assigned every terminal lot to
every order. RM-01 instead attributes accepted terminal output through the propagated
`order_id`, accumulates quantity, and records the exact time required quantity is met.
The CNC example expectation was updated because its old count of several late orders
depended on that incorrect cross-order lot attribution.

## Review findings resolved

- Terminal stock destinations remain stock destinations; only terminal list sinks are
  wrapped for fulfillment observation.
- Legacy hand-built event fixtures and the 12-column event schema remain compatible.
- Missing completion values remain `None`; the new distribution contract exposes
  censoring rather than silently estimating percentiles from survivors.
- `ScoringSurface` no longer calls `os.chdir`, and it retains evaluation artifacts.
- Process count is capped by the explicit `max_workers` value (default four).
- Public additions carry type annotations, files use context-managed I/O or pathlib
  helpers, and no broad exception suppression was added.

## Evidence and residual risk

Fresh verification on 2026-09-12:

- Focused RM-01, CLI, sweep, service, and compatibility checks: 43 passed.
- Full test suite: 510 passed with one upstream Starlette/httpx deprecation warning.
- Ruff on all changed Python files: clean; Ruff format check: clean.
- mypy strict project configuration: no issues in 63 source files.

These tests prove the implemented software contracts; they do not prove domain
validity, enterprise isolation across hosts, or accurate promise dates.

Remaining limitations are explicit in `USER-GUIDE.md`: shipped quantity currently
equals accepted quantity, unresolved work has one incomplete status, direct
`RunDriver` execution cannot retain the source model because it receives only a
compiled object, randomness is source-stream stable rather than keyed per entity and
operation, and wall limits are cooperative between SimPy events.
