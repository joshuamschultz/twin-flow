# RM-06 review

The scheduling package is isolated under `twinflow.scheduling` and does not
modify the simulator, service, or engine. The baseline enforces precedence,
resource qualification, finite windows, readiness gates, no overlap, and
frozen assignments, then runs an independent verifier over the result.

Focused tests cover hand-solved feasible and infeasible cases, qualification,
calendar limits, frozen conflicts, unsupported translation, ranking, and the
optional solver seam. Ruff, strict mypy, and pytest are the release checks.

The baseline is a deterministic feasible heuristic, not an optimizer proof.
OR-Tools is optional and intentionally not imported unless its adapter is
instantiated; when installed it builds CP-SAT interval, precedence, window,
qualification, and frozen-assignment constraints and reports native status and
objective bounds. The translator supports a narrow deterministic manufacturing
subset; stochastic distributions, quality gates, and unsupported rework are
reported as explicit issues.
