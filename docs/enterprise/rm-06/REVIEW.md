# RM-06 review

The scheduling package is isolated under `twinflow.scheduling` and does not
modify the simulator, service, or engine. Problem model v2 (`schema_version "2.0"`)
replaces the earlier flat model. `solve` validates the request, compiles overrides,
refuses features the solver does not declare, runs the solver, and then runs an
independent verifier over the result. Objective values are reported only for a
verified schedule.

The verifier shares no code with the solvers. It re-derives precedence, lags, setup,
calendar, operator, batch, pin, and override constraints from the model. Mutation
tests change one constraint at a time and each mutation must be caught.

Known-optimum fixtures are Pinedo Example 7.1.1 (makespan 28) and Example 14.2.1
(total weighted tardiness 98 at the order 2, 4, 3, 1). A brute-force oracle checks the
objectives and the verifier against them. CP-SAT reaches and proves the 28.

The baseline is a deterministic feasible heuristic, not an optimizer proof. It never
backtracks and reports `UNKNOWN`, not `INFEASIBLE`, when it cannot place an operation.
OR-Tools is optional and is not imported unless `ORToolsSolver` is built. CP-SAT
optimizes `makespan` and `max_lateness` and refuses problems that use setups,
batching, pause, restart, or overlap. The translator reads a scenario capsule into
lot operations and returns a named blocker for anything it cannot express:
stochastic times, quality gates, rework, shared machines, and similar.

A scale check covers 600 jobs of 8 operations each with setups, calendars and
operators. Ruff, strict mypy, and pytest are the release checks.
