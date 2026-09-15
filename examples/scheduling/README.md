# Scheduling worked example

A tiny hard-constraint job-shop problem: two operations, two resources,
one precedence link. `problem.json`:

```json
{
  "objective": "makespan",
  "operations": [
    {"id": "cut", "duration": 2, "required_qualifications": ["machinist"]},
    {"id": "assemble", "duration": 3, "predecessors": ["cut"], "required_qualifications": ["assembler"]}
  ],
  "resources": [
    {"resource_id": "machine-1", "start": 0, "end": 20, "qualifications": ["machinist"]},
    {"resource_id": "bench-1", "start": 0, "end": 20, "qualifications": ["assembler"]}
  ]
}
```

- **`operations`** &mdash; each has a duration, an optional list of
  `predecessors` (must finish first), and `required_qualifications` that
  whatever resource runs it must hold.
- **`resources`** &mdash; each is a window (`start`/`end`, in the same time
  units as durations) plus the `qualifications` it holds. `assemble` needs
  an `assembler`-qualified resource, so only `bench-1` is eligible for it.
- **`objective`** &mdash; `makespan` (finish everything as early as
  possible) or `lateness`.

## Run it

```bash
twinflow schedule examples/scheduling/problem.json --solver baseline --time-limit 10
```

```json
{"ends": {"assemble": 5.0, "cut": 2.0}, "objective_value": 5.0, "resources": {"assemble": "bench-1", "cut": "machine-1"}, "starts": {"assemble": 2.0, "cut": 0.0}, "status": "FEASIBLE", "verified": true}
```

`cut` runs on `machine-1` from 0-2, then `assemble` runs on `bench-1` from
2-5 (it has to wait for `cut` to finish). Makespan is 5.

## What the output means

- **`status`** &mdash; `FEASIBLE` means a schedule that satisfies every
  precedence, window, and qualification constraint was found. `OPTIMAL`
  (CP-SAT only, see below) means that schedule is also provably the best
  possible for the objective. `UNKNOWN` means no schedule could be placed
  (a resource window too short, a precedence cycle, or a solver time-out);
  `INFEASIBLE` means the problem itself is malformed before solving even
  starts.
- **`verified`** &mdash; whether an independent checker re-confirmed the
  returned `starts`/`ends`/`resources` actually respect every constraint,
  separate from whatever solver produced them. This is a correctness check
  on the answer, not a measure of how good the answer is.
- **`starts`** / **`ends`** / **`resources`** &mdash; the schedule itself:
  when each operation starts and ends, and which resource was assigned to
  it.
- **`objective_value`** &mdash; the makespan (or lateness) the schedule
  achieves.

`--time-limit` is optional for the baseline solver (it has nothing to time
out on); it matters for `--solver ortools` below.

## The CP-SAT option

`--solver ortools` solves the same problem with Google's CP-SAT constraint
solver instead of the deterministic greedy baseline, if the optional
`ortools` package is installed. `twinflow schedule --help` lists both solver
choices. If `ortools` is missing, `--solver ortools` fails with a clear
`install the 'ortools' extra to use ORToolsSolver` error instead of a result.

```bash
twinflow schedule examples/scheduling/problem.json --solver ortools --time-limit 10
```

```json
{"ends": {"assemble": 5.0, "cut": 2.0}, "objective_value": 5.0, "resources": {"assemble": "bench-1", "cut": "machine-1"}, "starts": {"assemble": 2.0, "cut": 0.0}, "status": "OPTIMAL", "verified": true}
```

On this two-operation problem both solvers land on the same schedule, but
CP-SAT proves it `OPTIMAL` instead of just `FEASIBLE` &mdash; on a larger
problem with many valid orderings, that proof is the difference CP-SAT
buys you. `--time-limit` caps how long CP-SAT searches before it has to
report its best answer so far (status `UNKNOWN` if it ran out of time
without proving anything).
