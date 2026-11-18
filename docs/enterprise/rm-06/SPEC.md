# RM-06 constrained scheduling: problem model v2

`twinflow.scheduling` takes one versioned request, `SolveRequest` with
`schema_version: "2.0"`, and returns a `SolveResult`. Every surface (Python, REST,
SDK, MCP, CLI) takes and returns the same two types. The checked-in JSON Schema
[`src/twinflow/scheduling/problem.schema.json`](../../../src/twinflow/scheduling/problem.schema.json)
is generated from the models by `request_json_schema()`, and a test fails when the
file drifts. Version 1 is gone, and nothing reads it.

All times are float seconds. All types are frozen Pydantic models that forbid unknown
fields and non-finite numbers. Ids are 1 to 200 characters, free text is at most 2,000
characters. Size bounds protect the boundary: 100,000 operations, 10,000 machines,
work centers, calendars, pools and setup functions each, 1,000,000 calendar intervals,
10,000 overrides. There is no piece cap and no operation cap beyond these bounds.

## Request

| Field | Meaning |
|---|---|
| `schema_version` | Must be `"2.0"`. Anything else gets the issue `schema_version`. |
| `problem` | The shop at `now`: jobs, machines, work centers, calendars, operator pools, setup functions. |
| `profile` | Plant policy (below). Omitted means the default profile. |
| `overrides` | Planner changes that compile into the problem before any solver runs. |
| `previous_plan` | The plan being revised. Drives the frozen zone and the churn objective. |

A runnable request is in [`examples/scheduling/problem.json`](../../../examples/scheduling/problem.json).

## Model

- **Job**: a customer order with `release`, `due`, `promised_ship`, a `weight_class`
  (1, 2 or 4) or a `weight_override`, a `status` (`open`, `held`, `done`), and its
  operations.
- **Operation**: one processing step of a whole lot of `quantity` units. A lot is
  never split into pieces. Each operation lists `options`, one per eligible machine,
  with `fixed_seconds`, `unit_seconds` and a `preference_cost`. Run time is
  `fixed + quantity * unit`.
- **Precedence**: end-to-gate link to a predecessor, with `min_lag`, an optional
  `max_lag`, and an optional `overlap_quantity` (a transfer lot: the successor may
  start when that much of the predecessor is done).
- **Machine**: unary. It runs one operation at a time. More capacity means more
  machines. A machine may have a calendar, a setup function, a batch capacity, a
  restart rule, hour costs and a `state` at `now` (when it frees up and how it was
  last set up).
- **Work center**: a group of machines. Its setup function applies to a machine that
  has none.
- **Calendar**: explicit working intervals `[start, end)`, sorted and disjoint. A
  machine or pool without a calendar always works. An operation with
  `shift_crossing: "forbid"` must fit inside one interval. With `"pause"` it pauses
  over breaks.
- **Operator pool**: interchangeable operators with a size and an optional calendar.
  An operation draws `quantity` operators from a pool during `setup`, `run` or `both`.
- **Setup function**: a closed set of typed terms, never an expression.
  `abs_diff` costs `seconds_per_unit * |a - b|` over a numeric parameter.
  `mismatch` costs `seconds` when a parameter differs. Terms combine by `sum` or
  `max`. A family matrix gives changeover seconds between setup families, and
  `default_seconds` is the fallback. Resolution order: parameters, then family
  matrix, then default.
- **Setup is the first phase of the operation.** The machine is busy on
  `[start, end)`. Setup runs on `[start, setup_end)` and the run on
  `[setup_end, end)`. `setup_detached: true` means material is needed only from
  `setup_end`, not from `start`.
- **Pin and status**: an operation can be pinned to a machine and a start window. An
  operation `in_progress` is placed at `now` on its machine for its
  `remaining_seconds`. A `done` operation is skipped and counts as finished.

## Overrides and the frozen zone

Overrides are compiled into the problem by `apply_overrides` before any solver runs.
Solvers, the verifier and the evaluator see only the compiled problem. Each override
has an `id`, a `fact_ref` and a `reason`, so every change is traceable.

| Kind | Effect |
|---|---|
| `lock` | Pin an operation to a machine and/or an exact start. |
| `expedite` | Raise a job to weight class 4 and optionally pull its due date in. |
| `priority` | Set a job's weight class or explicit weight (exactly one). |
| `exclude_machine` | Hard: remove the option. Soft: add `penalty` to it. |
| `prefer_machine` | Hard: keep only the option. Soft: add `penalty` to the others. |
| `split` | Split a job's lot into child jobs by fractions that sum to 1. |
| `hold` | Delay a job until `until`, or leave it out of this solve. |

The order is fixed (hold, split, priority and expedite, exclude, prefer, lock, then
by id), so the order of the list never changes the result. A conflict is reported,
never resolved silently. Issue codes: `override_unknown_target`,
`override_conflict`.

The profile's `frozen_zone` (`until`, `tolerance`) pins every operation of the
previous plan that starts before `until` to its machine, start plus or minus the
tolerance. A lock, or a hard machine removal, on such an operation wins and releases
it. The result lists `released_from_frozen`. A frozen zone without a
`previous_plan` gets `frozen_requires_previous_plan`.

## Scheduling profile

`SchedulingProfile` is the plant policy:

- `class_weights`: weight per class, default `{1: 1, 2: 2, 4: 4}`. The keys must be
  exactly 1, 2 and 4.
- `objective`: an objective spec. Default: lexicographic makespan.
- `frozen_zone`, `horizon_end`.
- `time_budget_seconds`: default 10, at most 300.
- `robustness_load_targets`: default `(0.9, 0.8, 0.7)`, not increasing.
- `default_portfolio`: default `("baseline",)`.

`load_profile(text)` reads a TOML profile and validates it. Omitted fields take the
defaults. Unknown keys are rejected. `default_profile()` returns the default.
Signing of profile files is deferred to TF-003.

```toml
id = "plant-a"
time_budget_seconds = 30

[class_weights]
1 = 1.0
2 = 2.0
4 = 5.0

[objective]
mode = "lexicographic"
levels = [{ kind = "max_lateness" }, { kind = "makespan" }]
```

## Objectives

There are thirteen objectives (`ObjectiveKind`). All are minimized except
`robustness`. A verified result reports all thirteen. Times are measured from `now`.
A job counts only when at least one of its operations is assigned, and its
completion `C_j` is the latest `end`. Jobs without a due date are left out of every
due-date term. Empty sets give 0.

| Kind | Meaning |
|---|---|
| `makespan` | Latest completion minus `now`. |
| `weighted_completion` | Sum of `w_j * C_j`. |
| `max_lateness` | Largest `C_j - d_j`. |
| `tardy_count` | Number of late jobs. |
| `weighted_tardy_count` | Sum of `w_j` over late jobs. |
| `total_tardiness` | Sum of `max(0, C_j - d_j)`. |
| `weighted_tardiness` | Sum of `w_j * max(0, C_j - d_j)`. |
| `earliness_tardiness` | Sum of `abs(C_j - d_j)`. |
| `total_setup` | Sum of setup seconds (`setup_end - start`). |
| `cost` | Run and setup seconds times machine hour costs, in cost units per hour. |
| `churn` | Operations whose machine changed or whose start moved by more than the frozen-zone tolerance, against `previous_plan`. |
| `robustness` | `R(S) = sum w_j (d_j - C_j) / sum w_j (d_j - now)` (Pinedo p.483). Maximized. |
| `soft_penalty` | Sum of `preference_cost` of the chosen options. |

The effective weight is `weight_override` if set, else the profile's class weight.

There are three objective specs, chosen by `mode`:

- `weighted_sum`: terms of `kind`, `weight` and optional `scale`.
- `lexicographic`: levels in strict order. Each level has a `tolerance`, absolute or
  relative, that later levels may not worsen.
- `epsilon_constraint`: a `primary` objective with upper `bounds` on others.

`rank(candidates, spec)` orders candidate value sets under a spec. Ties break by id,
so the order never depends on input order.

## Solvers and capabilities

`Solver` is a protocol: `name`, `capabilities`, and
`solve(problem, objective=, budget_seconds=, context=)`. The solver gets the compiled
problem, the objective spec and a `SolveContext` (see "The seam"), never the overrides
or the profile.

`solve(request, solver=None, budget_seconds=None)` is the one entry point:

1. Validate the request.
2. Compile the overrides and validate the result.
3. Derive the `Feature` set the problem uses (`problem_features`).
4. Refuse any feature the solver does not declare, with one `unsupported_feature`
   issue per feature. The solver is not called.
5. Run the solver.
6. Verify any `FEASIBLE` or `OPTIMAL` answer with the independent verifier.
7. Evaluate all thirteen objectives of a verified schedule.

A refusal returns an `UNKNOWN` schedule with the issues. `verified` is true only when
the solver answered `FEASIBLE` or `OPTIMAL` and the verifier found no issue. A solver
exception is a solver bug and propagates.

Features: `alternatives`, `setups`, `detached_setup`, `calendars`, `pause`,
`operators`, `batching`, `restart`, `lags`, `max_lags`, `overlap`, `pins`,
`in_progress`, `machine_state`.

| Solver | Name | Declares | Optimizes |
|---|---|---|---|
| Baseline | `baseline` | every feature | A deterministic list scheduler. No objective is optimized. |
| CP-SAT | `ortools` | every feature except `pause` | Every objective kind, as one model (see "Algorithm library"). |

- The baseline never backtracks. When it cannot place an operation it returns
  `UNKNOWN` with `max_lag_unmet` or `no_feasible_slot`, never `INFEASIBLE`. It
  reports `FEASIBLE`, never `OPTIMAL`. It does not enforce the time budget.
- CP-SAT is the optional `scheduling` extra (`ortools>=9.10,<10`). Importing
  `twinflow.scheduling` never imports OR-Tools. Building `get_solver("ortools")`
  without it raises `OptionalDependencyError` (in `twinflow.scheduling.seam`). It reports `OPTIMAL` with a proven bound,
  `INFEASIBLE`, or `UNKNOWN` on a time limit. Times with more than six decimals get
  `unsupported_precision`. A solver issue code is also `infeasible`, `time_limit` or
  `model_too_large`.
- `FEASIBLE` is a verified schedule, not an optimality claim.

## Algorithm library

TF-003 adds Pinedo's methods (*Scheduling*, 5th ed.) behind the same `Solver` seam.
Each method is a leaf module. Every plan goes through one decoder and the independent
verifier, so a method cannot return an unchecked schedule.

### Registry and leaf rule

`twinflow.scheduling.registry` finds the methods (D-069). It scans the modules of
`twinflow.scheduling.algorithms` (`pkgutil.iter_modules`) and imports each. A module
exports `SOLVERS`, and may export `RULES` and `ANALYSES`. `baseline` registers from
core. The facade imports no algorithm module.

| Call | Result |
|---|---|
| `registry.available()` | Every solver: name, family, capabilities, module, `deterministic`. Facade name: `available_solvers`. |
| `registry.get(name, **params)` | A fresh solver. Facade name: `get_solver`. An unknown name raises `UnknownSolverError`. |
| `registry.is_registered(name)` | Whether a name is registered. The optional dependency may still be absent. |
| `registry.rule(name, **params)` | A priority rule by name (`UnknownRuleError`). Used by construction and beam. |
| `registry.analysis(name)` | A screen function by name (`UnknownAnalysisError`). |

- Optional dependencies are imported inside the factory. A missing one raises
  `OptionalDependencyError` at build time, never at discovery. Any other import error
  in a leaf propagates. A duplicate name is an error.
- Leaf modules never import each other, and they never import `solver.py`. A method
  that uses another one looks it up by name at call time. A missing leaf gives a named
  issue (`subsolver_unavailable`, `unknown_solver`), not an import error.
- Delete any file in `algorithms/` and the rest still imports, registers and runs.
  An architecture test deletes each leaf in turn and checks this.

### The seam

`twinflow/scheduling/seam.py` is a leaf that imports only `model`. It holds `Feature`,
`Solver`, `SolveContext`, `ParamValue` and `OptionalDependencyError`. Solvers and the
kernel import the seam, so they never import `solver.py`.

`SolveContext` carries what a method needs besides the problem: the previous plan, the
churn tolerance, a seed, a monotonic deadline, an optional warm-start schedule, and
`params` (a read-only mapping from `profile.method_params[solver.name]`). `solve()`
builds it from the request. A method that gets an unknown or invalid param returns
`UNKNOWN` with `invalid_method_param`.

### Methods

Every method works on the full problem model. All declare every `Feature`; `ortools`
declares every one except `pause`. All are deterministic except `sa`. Gate: the D-083
gate (below); `ref` marks a reference rule that is exempt, `n/a` a method that is
timed but not gated. The 600x8 column is the measured time on the scale instance
(600 jobs of 8 operations, 8 centers); the budget is the limit from the runtime table.
Pinedo page numbers are the printed pages.

| Name | Family | Module | Key params (default) | Budget / 600x8 | Gate | Pinedo |
|---|---|---|---|---|---|---|
| `baseline` | baseline | `baseline` | none | none / n/a | n/a | n/a |
| `fifo`, `edd`, `wspt`, `spt`, `lpt`, `ms`, `cr`, `sst`, `lfj`, `sqno` | dispatch | `dispatch_rules` | none | 10 s / 0.66 to 1.05 s | ref | p.372-375 |
| `atc` | composite | `atcs` | `K1`, `phi` | 10 s / 0.81 s | PASS | p.199, 374-375 |
| `atcs` | composite | `atcs` | `K1`, `K2` (from the K-table or formula), `phi`, `flexibility`, `suitability`, `macro_jobs`, `merge_window`, `mode` | 10 s / 1.08 s | PASS | p.375-378 |
| `gt_active` | construction | `construction` | `rule` (`atcs`) | 15 s / 1.42 s | PASS | p.372-375, 523 |
| `forward_load` | construction | `construction` | `rule` (`atcs`) | 10 s / 0.81 s | PASS | p.183-184, 473-474 |
| `backward_load` | construction | `construction` | `rule` (`atcs`) | 10 s / 2.68 s | PASS | p.183-184, 473-474 |
| `five_phase` | decomposition | `five_phase` | `phi`, `k2_scale` (6) | 20 s / 10.83 s | PASS | Sec 16.4, p.443-447 |
| `sb_makespan` | decomposition | `shifting_bottleneck` | `subproblem` (10), `reopt_rounds` (2), `K2` (1.0) | 90 s / 15.24 s | PASS (makespan bench) | Alg 7.2.1, p.189-195 |
| `sb_wt` | decomposition | `shifting_bottleneck` | same as `sb_makespan` | 90 s / 19.71 s | PASS | Sec 7.3, p.196-202 |
| `rolling_horizon` | decomposition | `rolling_horizon` | `window_solver` (`atcs->tabu`), `min_jobs` (20), `max_jobs` (150), `seam` (5), `gap` (1.0), `eps` (0.25) | 60 s / 24.30 s | PASS | Sec 15.2, p.398-403 |
| `ortools` | exact | `cpsat` | `hint_solvers` (`atcs`) | 3 s gate; 600x8 refused in 0.29 s (`model_too_large`, limit 2 s); 40x5 window 5.06 s (limit 5.5 s) | PASS | not in the book (exact reference) |
| `tabu` | improvement | `local_search` | `tabu_length` (7), `iteration_limit` (30) | anytime: 28.62 s at 30 s | PASS | Alg 14.3.3, p.382-384 |
| `sa` | improvement | `local_search` | `sa_cooling` (0.995), `sa_iterations` (20,000) | anytime: 28.69 s at 30 s | PASS | Alg 14.3.2, p.380-382 |
| `beam` | improvement | `beam` | `beam_width` (4), `filter_width` (12), `rollout_limit` (60), `rule` (`atcs`) | anytime: 28.58 s at 30 s | PASS | Sec 15.1, p.396-398 |
| `rush_insert` | reactive | `reactive` | none | 10 s / 0.90 s | n/a | Sec 18.1 p.482; Sec 16.5 p.448-449 |
| `reschedule` | reactive | `reactive` | `improver` (`tabu`), `churn_weight` (1.0) | budget + 10 percent / 28.63 s at 30 s | n/a | Sec 18.1 p.482 |

- `atcs` picks K1 and K2 per work center from its statistics (`tau`, `R`, `eta`, `m`).
  Order: explicit params, then a `k_lookup` given to the constructor, then the
  formula. A K-table plugs in through `k_lookup` (see "The K-table").
- `ortools` hints from the best verified plan among the baseline and `hint_solvers`, and
  never returns a plan worse than that hint. It refuses a model with more than 300
  options on one machine or 2,000,000 circuit arcs (`model_too_large`).
- `sb_makespan` is gated on a makespan bench, not on weighted tardiness (a makespan
  method cannot be expected to beat EDD on weighted tardiness).
- `rolling_horizon` is weaker than plain `atcs` when it needs more than one window
  (the windows queue behind each other). The held-out gate passes because 40-job
  instances form one window. Use `atcs` or `sb_wt` for large instances.
- `sa` is the only non-deterministic method: it takes the seed from the context.
- `beam` stops after `rollout_limit` rollouts, so its output does not depend on the clock.
- `rush_insert` needs a previous plan (`previous_plan_required` otherwise).
  `reschedule` uses the previous plan as a warm start and counts churn.

### Reactive functions

`rush_insert` places one new job into the previous plan. It picks the position by a
graph estimate and confirms it by decoding. When the new job would make a job late
that was on time, it relaxes that job's due date, in order, until the plan holds. Each
relaxation is in `Schedule.relaxations` and has a `relaxed_due` issue. Added setup is an
informational `rush_added_setup` issue.

`reschedule` keeps the part of the previous plan that no moved or new operation can
reach, schedules the rest with ATCS, and improves with `improver`. It adds churn to the
objective: a weighted-sum term, a last lexicographic level, or nothing for an epsilon-constraint spec. `churn_weight` is a method param (default 1.0). Without a
previous plan it returns `UNKNOWN` with `previous_plan_required`.

### Portfolio and Pareto menu

`solve_portfolio(request, budget_seconds=None, seed=0, pipelines=None, k_lookup=None)`
returns a `PortfolioResult`: candidates, a Pareto menu, a recommendation, the effective
objective, the load ratio and ATCS run records.

- **Pipelines.** A pipeline is a string `a->b->c` of registry names. Each stage gets the
  previous verified schedule as its warm start. Every stage result is a candidate
  (`00:atcs`, `00:atcs->tabu`). The default is `profile.default_portfolio`. With
  `profile.workers` above 1 the pipelines run in a spawn-context process pool. Results
  are ordered by pipeline index, so a deterministic method gives the same answer with any
  worker count. Bounds: at most 64 pipelines of at most 8 stages (`too_many_pipelines`,
  `pipeline_too_long`). No pipeline runs past 90 percent of the budget.
- **Load-dependent weights.** `load_ratio` is the largest ratio over work centers of
  (shortest run time plus setup) to working seconds before the horizon. When
  `profile.load_weights` is set, the weight of each objective term moves from its light
  value to its heavy value as the ratio goes from `light_load` (0.6) to `heavy_load`
  (0.95). Without a horizon the ratio is `None` and the light weights apply. An infinite
  ratio is stored as `None` with a `load_ratio_unbounded` issue, and the heavy weights
  apply.
- **Pareto menu.** The menu holds the non-dominated verified candidates over
  `profile.menu_axes` (2 or 3 objective kinds; default weighted tardiness and total
  setup). `MenuEntry.supported` is true when some positive weighting of the axes makes the
  point optimal (convex hull for 2 axes, a linear program for 3). A point that is
  non-dominated but not supported is an unsupported point: a weighted-sum sweep can
  never find it, so the menu keeps it and Pareto search looks for it. Pareto search starts
  from the verified `dispatch` and `composite` candidates and tries adjacent swaps on
  every machine. Its points are the candidates `pareto:{n}`.
- **Recommendation.** `recommended` is the best verified candidate under the effective
  objective (`rank`). It is `None`, with `no_verified_candidate`, when nothing verified.
- **Churn.** When the request has a previous plan and `churn_weight` is above 0, the
  effective objective gets a churn term.

Planning functions for TF-005 (pure, typed, no I/O; exported by the facade):

| Function | Does |
|---|---|
| `solve_portfolio(request, ...)` | The portfolio above. |
| `get_pareto_menu(result, axes=None)` | The stored menu, or one recomputed for other axes from the stored candidate values. No solver runs. |
| `resolve_with_overrides(request, previous_plan, overrides, ...)` | Adds the overrides and the previous plan, then runs `reschedule` first and the profile portfolio. |
| `insert_rush_order(request, previous_plan, rush_job, ...)` | Appends the job and solves with `rush_insert`. Returns a `SolveResult`. |

Measured on the scale instance: `solve_portfolio` 57.91 s (limit 133 s at a 120 s
budget), Pareto search 30.03 s (limit 33 s at a 30 s budget).

### The K-table

ATCS needs two look-ahead constants per work center. `twinflow.scheduling.learning`
keeps a table of them (K1, K2 by `tau`, `R`, `eta` and machine count) and improves it
from runs (Pinedo Example 18.2.1, p.488-489).

- `KTable`, `seed_table()`, `lookup()`, `as_k_lookup(table)` (a picklable callable that
  ATCS accepts as `k_lookup`).
- `perturbation_runs(request, center, table, delta)` runs ATCS at the table K and at 8
  neighbors of one center. `update_k_table(...)` is pure: it changes the nearest entry
  only when the best run beats the base by the threshold. No update when the base value
  is 0 or below. Default update radius 0.05 (trained on seeds 0-9, checked on 10-19).
- `KTableStore(path)` is the store seam: it writes atomically and checks a SHA-256
  digest of the entries on load (`KTableIntegrityError` on a mismatch).
  The digest detects accidental damage. It is not a signature: anyone who can write the
  file can also rewrite the digest. A deployment that needs tamper evidence must sign
  or protect the file itself.

### Quality gates

D-083 defines the gate. Score = weighted tardiness plus 0.2 times total setup, both
divided by the EDD schedule's values (the default bench profile). A method passes when,
on 30 held-out generated HMLV instances (40 jobs x 5 operations x 4 centers, seeds
100-129):

1. its mean score is below the mean of FIFO, EDD and SPT, and
2. it beats the best of the three on at least 18 of 30 instances, and
3. this holds for each of seeds 0 to 4, with a coefficient of variation of the mean
   score over seeds of at most 0.05. A method registered `deterministic=True` runs once
   and must give identical output for different seeds on 3 instances.

Elementary dispatch rules are the comparison set and are exempt. Defaults are tuned on
the training seeds (0-19) only. Gate budgets per instance: `ortools` 3 s, `tabu` 2 s,
`sa` 0.5 s, `beam` 1 s, portfolio 30 s (bench portfolio `atcs`, `atcs->tabu`, `sb_wt`,
`edd`, one worker). Measured gate table (mean score over EDD, wins of 30, seed CV):

| Method | Score | Wins | CV | Result |
|---|---|---|---|---|
| `atc` | 1.0029 | 26/30 | 0 | PASS |
| `atcs` | 0.9508 | 26/30 | 0 | PASS |
| `atcs` + flex + suit + macro | 1.0442 | 25/30 | 0 | PASS |
| `gt_active` (rule `atcs`) | 1.1200 | 20/30 | 0 | PASS |
| `forward_load` (rule `atcs`) | 1.0272 | 26/30 | 0 | PASS |
| `backward_load` (rule `atcs`) | 0.9054 | 28/30 | 0 | PASS |
| `sb_wt` | 1.0312 | 26/30 | 0 | PASS |
| `sb_makespan` (makespan bench) | 0.8671 | 21/30 | 0 | PASS |
| `five_phase` (flow-line bench) | 0.9394 | 28/30 | 0 | PASS |
| `rolling_horizon` | 0.9337 | 26/30 | 0 | PASS |
| `ortools` (3 s) | 0.9508 | 26/30 | 0 | PASS |
| `tabu` (2 s) | 0.9337 | 26/30 | 0 | PASS |
| `sa` (0.5 s, seeds 0-4) | 0.9498 | 26/30 | 0.0003 | PASS |
| `beam` (1 s) | 0.7688 | 30/30 | 0 | PASS |
| portfolio (30 s) | 0.8838 | 30/30 | 0 | PASS |

Reference rules (mean score, wins): `wspt` 1.1960 (19), `edd` 1.2000 (0), `cr` 1.3231
(8), `ms` 1.3072 (11), `sst` 1.3406 (9), `lfj` 1.4573 (3), `spt` 1.5241 (0), `sqno` 1.6361
(2), `lpt` 2.0994 (0), `fifo` 2.1052 (0).

Notes on the table:

- A score under 1 beats the EDD schedule. A score near 1.0 to 1.1 still wins on the
  count, because the score mixes tardiness and setup.
- `ortools` and `tabu` and `sa` pass on the strength of their ATCS start or hint: at
  these budgets they barely improve it. The gate checks that they never return worse.
- `sb_makespan` and `five_phase` use their own bench (a makespan bench, a flow-line
  bench). `rush_insert` and `reschedule` are not gated (the bench instances have no
  previous plan). They are timed and reached through the portfolio.
- The runtime table above is checked by the `slow` runtime tests (112 slow tests passed in 631 s).

Contract fuzz: every registered solver runs on seeds 0 to 1499 of the seeded problem
generator (0-199 in the default run, 200-1499 under the `slow` marker; CP-SAT 0.25 s per
seed). Every `FEASIBLE` or `OPTIMAL` answer must verify, and every refusal must be a
named issue. Seeds with `seed % 5 == 1` carry a previous plan and a frozen zone, so
`rush_insert` and `reschedule` are exercised.

Code size: `src/twinflow/scheduling` grew from 5,038 to 17,083 lines (core 5,356, kernel
2,971, algorithms 7,506, portfolio 914, learning 336). Tests grew from 9,086 to 27,821
lines.

## Independent verifier

`verify(compiled, overrides, schedule)` re-derives every hard constraint from the
model. It shares no code with any solver, so a solver bug cannot hide behind the code
that checks it. All time comparisons allow 1e-6 seconds. Work is O(n log n).

| Issue code | What failed |
|---|---|
| `nonfinite` | A non-finite time or primary value. |
| `unknown_assignment` | An unknown operation, or one assigned twice. |
| `held_scheduled` | An operation of a held job was scheduled. |
| `missing_assignment` | A live operation has no assignment. |
| `machine_overlap` | Two operations overlap on one machine. |
| `ineligible_machine` | The machine is not an option of the operation. |
| `in_progress` | An in-progress operation is not at `now` on its machine. |
| `duration` | The run length is not `fixed + quantity * unit`. |
| `setup` | The setup length differs from the setup function. |
| `calendar` | Work outside the machine calendar. |
| `operator_calendar` | Operator work outside the pool calendar. |
| `readiness` | Start before the machine is free at `now`, or the material gate before job release or operation ready time. |
| `pin` | A pin is not respected. |
| `precedence` | A predecessor gate is not respected. |
| `max_lag` | A `max_lag` is exceeded. |
| `overlap_lag` | A transfer-lot gate is not respected. |
| `operator_capacity` | A pool is over capacity. |
| `batch` | A batch breaks its family or capacity rule. |
| `override` | A hard override is not respected. |
| `objective_mismatch` | The reported primary value differs from the recomputed one. |

## Validator

`validate(request_or_compiled)` returns every issue, never only the first. Each issue
has a `code`, a JSON `path` such as
`problem.jobs[0].operations[1].options[0].machine_id`, and a `message`.
`parse_request(json_or_mapping)` is the input boundary: it returns a `SolveRequest`
or a list of issues, so callers never see a raw `ValidationError`.

Codes: `schema`, `schema_version`, `duplicate_id`, `unknown_reference`, `cycle`,
`no_options`, `option_outside_work_center`, `lag_bounds`, `overlap_requires_forbid`,
`batch_rules`, `pin_outside_options`, `pin_window`, `in_progress_machine`,
`restart_rule`, `calendar_order`, plus the override codes above.

## Contracts

**REST.** `POST /api/workspace/schedules` takes
`{"request": <SolveRequest>, "backend": "baseline" | "ortools", "time_limit": 0..30}`
and returns 201 with the record. An invalid request returns 422 with the issue list.
A body with a top-level `problem` (version 1) returns 422 and names `schema_version`.
A missing OR-Tools install returns 503. The record keeps `schema_version`,
`objective_spec`, `objective_values`, `features`, `held_job_ids`, `verified`, the
request and the full `SolveResult`.

`POST /api/workspace/schedule-snapshots/{id}/solve` keeps its path. It translates the
snapshot's scenario capsule into a v2 request (translator `capsule-ops-2`): locations
become work centers, capacity N becomes N unary machines, each order and routing step
becomes one lot operation, routing becomes precedence, a labor skill becomes one
operator from its pool, labor calendars become explicit intervals, and `setup_key`
with `changeover_seconds` becomes a family-matrix setup function. There is no piece or
operation cap. What the model cannot express returns `status: "blocked"` with one
named issue per feature, never a silent drop. Blocker codes: `material_stock`,
`release_policy`, `shared_machine`, `rework_cycle`, `skill_without_pool`,
`skill_in_several_pools`, `labor_absence`, `distribution_time_model`,
`batch_hold_time_model`, `quality_gate`, `split_output`, `batch_size`,
`machine_breakdown`, `unsupported_shift_crossing`, `multiple_outputs`,
`non_unit_ratio`, `unknown_product`, `scenario_mismatch`, `invalid_scenario`.
`GET /api/workspace/tool-contracts` lists `scheduling.schema_version`, backends,
features and objectives, and carries the v2 `scheduling_example`.

**SDK.** `TwinflowClient.schedule(request, backend=, time_limit=)` and
`solve_schedule_snapshot(snapshot_id, backend=, time_limit=)`.

**MCP.** `schedule_operations(request, backend, time_limit)` and
`solve_schedule_snapshot`. Both proxy the REST routes.

**CLI.** `twinflow schedule request.json --solver baseline|ortools --time-limit S`
prints the `SolveResult` JSON. Exit code 0 means verified. Exit code 1 means not
verified. An invalid request exits with a usage error that lists the issues.

**Python.**

```python
from twinflow.scheduling import parse_request, solve

parsed = parse_request(open("examples/scheduling/problem.json", "rb").read())
assert not isinstance(parsed, list)  # a list is the issues
result = solve(parsed)
assert result.verified
```

## Known-optimum fixtures

Two textbook problems from Pinedo, *Scheduling* (5th ed.), live in
`tests/scheduling/fixtures/`. A brute-force oracle (`tests/scheduling/oracle.py`)
checks the verifier and the objectives against them. CP-SAT must reach the optimum
and prove it.

- Example 7.1.1 (`pinedo_7_1_1.json`): job shop, four jobs, optimum makespan 28.
- Example 14.2.1 (`pinedo_14_2_1.json`): single machine with sequence-dependent
  setups, optimum total weighted tardiness 98 at the job order 2, 4, 3, 1.

## Limits

- The baseline is a feasible heuristic, not an optimizer.
- CP-SAT declares every feature except `pause`. A problem that uses `pause` is refused
  by CP-SAT with `unsupported_feature`. It refuses a model above its size guard with
  `model_too_large`, so the whole 600x8 instance goes to the heuristics or to
  `rolling_horizon` windows.
- The heuristics (dispatch, ATCS, construction, shifting bottleneck, local search) are
  anytime or fast, but they prove no optimum.
- `rolling_horizon` with more than one window scores worse than plain `atcs`.
- A schedule is deterministic evidence. Stochastic delivery confidence stays an
  application concern.
