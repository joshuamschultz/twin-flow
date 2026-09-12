# Constrained schedules

```python
from twinflow.scheduling import Operation, ResourceWindow, SchedulingProblem, solve, verify_schedule

problem = SchedulingProblem(
    operations=(
        Operation("cut", 2, required_qualifications=frozenset({"machinist"})),
        Operation(
            "assemble", 3, predecessors=("cut",), required_qualifications=frozenset({"assembler"})
        ),
    ),
    resources=(
        ResourceWindow("m1", 0, 20, frozenset({"machinist"})),
        ResourceWindow("a1", 0, 20, frozenset({"assembler"})),
    ),
)
candidate = solve(problem)
assert candidate.verified
assert verify_schedule(problem, candidate).valid
```

`status="FEASIBLE"` means a verified candidate was found. `OPTIMAL` is
reserved for a solver that proves an objective bound. Frozen operations must
fit their declared resource and start exactly. Windows are finite and work
cannot cross their end. Use `rank_candidates` and `pareto_frontier` to compare
verified alternatives; these helpers do not claim stochastic performance.
