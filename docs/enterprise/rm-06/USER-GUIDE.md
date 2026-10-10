# Constrained schedules

A schedule request is one JSON document, `schema_version: "2.0"`. See the
[RM-06 spec](SPEC.md) for every field. A small request is in
`examples/scheduling/problem.json`.

```python
from pathlib import Path

from twinflow.scheduling import parse_request, solve

parsed = parse_request(Path("examples/scheduling/problem.json").read_bytes())
if isinstance(parsed, list):  # a list means the request has issues
    raise SystemExit([issue.model_dump() for issue in parsed])
result = solve(parsed)
assert result.verified
print(result.schedule.status, result.objective_values)
```

From the command line:

```console
twinflow schedule examples/scheduling/problem.json --solver baseline --time-limit 10
```

The command prints the `SolveResult` JSON. Exit code 0 means the schedule passed the
independent verifier.

A job is an order made of lot operations. An operation lists the machines that can run
it. Each machine runs one operation at a time. Add calendars for working time, operator
pools for people, and setup functions for changeovers. Use `overrides` for planner
changes (lock, expedite, priority, exclude or prefer a machine, split, hold) and a
profile `frozen_zone` to keep the near-term part of a previous plan.

`status="FEASIBLE"` means a verified schedule was found. `OPTIMAL` is reserved for a
solver that proves a bound. The baseline never claims `OPTIMAL`. The optional CP-SAT
backend (`--solver ortools`, needs the `scheduling` extra) proves optimality for
`makespan` and `max_lateness` only. If your request uses a feature the solver does not
support, `solve` returns `UNKNOWN` with an `unsupported_feature` issue for each one.
It never drops a constraint.

Every verified result reports all thirteen objective values. Frozen zones need a
`previous_plan`. Use `rank` to order several verified candidates under an objective
spec. These helpers do not claim stochastic performance.
