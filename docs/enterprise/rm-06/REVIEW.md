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

Real CP-SAT evidence (OR-Tools 9.15.6755, one worker, seed 7): the hand-solved
precedence case returned `OPTIMAL` with objective and bound `5`; a 2-unit job
in a 1-unit window returned `INFEASIBLE`; a 0.0015-duration frozen operation
returned `OPTIMAL` and verified at the requested precision; a lateness objective
returned value `1`; and a 1e-9-second limit returned `UNKNOWN` with no claimed
candidate. The focused suite contains 11 passing tests covering these cases.
