# RM-06 constrained scheduling

RM-06 adds a typed scheduling seam for hard-feasible candidate generation. A
problem contains operations, finite qualified resource windows, precedence,
release/material/document readiness, frozen assignments, an objective, and an
optional deadline. The deterministic baseline schedules each ready operation
at its earliest feasible time and then independently verifies every hard
constraint.

`Solver` is a protocol. `solve` uses the always-available heuristic; an
optional CP-SAT adapter can be supplied without importing OR-Tools in the core
package. Solver statuses are `FEASIBLE`, `OPTIMAL`, `INFEASIBLE`, or `UNKNOWN`;
the heuristic reports `FEASIBLE` and no unjustified optimality bound.

The translator accepts only deterministic `rate_based` model locations and
explicit simple routes. It refuses distributions, quality gates, unsupported
rework, and missing routes instead of dropping semantics. Candidate ranking and
Pareto helpers operate on independently verified schedules; stochastic
reevaluation remains an application concern.
