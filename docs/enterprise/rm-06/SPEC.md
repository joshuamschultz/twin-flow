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
`solve(problem, objective=, budget_seconds=)`. The solver gets the compiled problem
and the objective spec only, never the overrides or the profile.

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
| Baseline | `baseline` | every feature except `restart` | A deterministic list scheduler. No objective is optimized. |
| CP-SAT | `ortools` | `alternatives`, `calendars`, `operators`, `lags`, `max_lags`, `pins`, `in_progress`, `machine_state` | `makespan` and `max_lateness` as the primary objective. |

- The baseline never backtracks. When it cannot place an operation it returns
  `UNKNOWN` with `max_lag_unmet` or `no_feasible_slot`, never `INFEASIBLE`. It
  reports `FEASIBLE`, never `OPTIMAL`. It does not enforce the time budget.
- CP-SAT is the optional `scheduling` extra (`ortools>=9.10,<10`). Importing
  `twinflow.scheduling` never imports OR-Tools. Building `ORToolsSolver()` without
  it raises `OptionalDependencyError`. It reports `OPTIMAL` with a proven bound,
  `INFEASIBLE`, or `UNKNOWN` on a time limit. Other primary objectives get
  `unsupported_objective`. Times with more than six decimals get
  `unsupported_precision`. A solver issue code is also `infeasible` or `time_limit`.
- `FEASIBLE` is a verified schedule, not an optimality claim.

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
- CP-SAT optimizes only `makespan` and `max_lateness`. A problem that uses setups,
  detached setups, pause, batching, restart or overlap is refused by CP-SAT with
  `unsupported_feature`. The baseline schedules all of them except `restart`.
  CP-SAT models for them are TF-003 work.
- `restart` is declared by no solver.
- A schedule is deterministic evidence. Stochastic delivery confidence stays an
  application concern.
