# CLI reference

Every command returns a process exit code (0 = success) and never raises past the
boundary. Run inside your virtualenv (`. .venv/bin/activate`).

## `twinflow validate <model>`

Check a `model.yaml` before running. Prints one line per problem found (path +
message); exit 0 means the model may run.

```bash
twinflow validate examples/cnc-shop/model.yaml
```

## `twinflow run <model> --plan <plan> --reps N`

Simulate `N` replications and write a run folder under `runs/<run-id>/` containing
`report.html`, `kpis.json`, `intervals.json`, and `run_meta.json`, plus the event and
resource tables described in [data contracts](data.md).

```bash
twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30
```

Use `--reps` greater than 1 to estimate confidence intervals — the report shows a
mean and interval for supported aggregate metrics.

The console summary shows completed orders out of the total, on-time count and percentage,
late count, and makespan in hours. With multiple replications these console figures describe
replication 1 (the representative run), marked `rep 1 of N`; the report's aggregate
intervals use all replications. With `--reps 1`, there is no `rep 1 of 1` note and no
multi-replication interval. If orders remain incomplete, the console prints a warning with
possible causes, including reaching the horizon, quality-gate scrap, or a resource deadlock.

Add `--orders` to print one line per order after the summary: its due time, the typical
finish (the middle of the runs), the bad-case finish (9 runs in 10 are done by then), and
how many runs it was late in. Then the three busiest work centers, which point at the
bottleneck. When the shop file has a work calendar, the times print as dates.

```bash
twinflow run shop.yaml --plan data/open-orders.csv --reps 20 --orders
```

A plan may give `start_date` and `due_date` as calendar dates (`2026-11-20` or
`2026-11-20 15:30`) when the shop file has a work calendar. The calendar's `origin` is
time zero. A due date with no time means the end of that day; a start before the origin
releases at once. See [`examples/showcase/cnc-job-shop/`](../examples/showcase/cnc-job-shop/).

## `twinflow scenario run <capsule> [--reps N] [--seed S] [--out DIR] [--summary]`

Run a capsule and print its result as JSON. With `--summary`, print a few plain lines
instead: profit per run for a make-to-stock plant, the on-time share for a shop, or one
line per customer order (typical and bad-case ready dates, chance of on time, short
parts) for a supply network. The last line names the full `result.json`.

A capsule opened from a folder may point at input files next to it. `model: shop.yaml`
reads the model from a YAML file. A list one level inside `model` or `snapshot` may be a
CSV file name, such as `products: data/products.csv` or `orders: data/customer-orders.csv`.
Each row is one item; numbers become numbers, a blank cell leaves the field out, and a
header like `delay_risk.maximum_days` makes a nested field. Every file must be inside the
capsule's folder. See [`examples/showcase/`](../examples/showcase/).

## `twinflow balance <model> --plan <plan> --sweep <sweep.json> --reps N`

Run a declared lever grid and report every point side by side, fairly paired with
common random numbers. The sweep file is plain JSON:

```json
{ "labor.pools[0].headcount": [2, 3, 4] }
```

```bash
twinflow balance examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --sweep sweep.json --reps 30
```

The command prints and writes `summary.json` in the sweep output directory. Each point
includes its lever values, replication count, mean `on_time_pct`, and mean `busy_hours`
(the legacy sweep's total machine `run_hours`).

## `twinflow report <run-id> --out html`

Confirm (and locate) the `report.html` for an already-completed run.

## `twinflow optimize <model> --plan <plan> --lever PATH:MIN:MAX[:STEP] ...`

Search an integer lever space for the scenario a named objective prefers, via a named
optimizer, and print the winner with its confidence band. Repeat `--lever` for more
than one knob. The twin scores the options; it proposes no commit — the decision stays
yours.

```bash
twinflow optimize examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --lever labor.pools[0].headcount:2:6 \
  --objective on_time_pct --optimizer hill_climb --budget 12 --reps 12
```

| Flag | Default | Meaning |
|---|---|---|
| `--lever` | (required) | `PATH:MIN:MAX[:STEP]`; a dot-path lever and its integer search range |
| `--objective` | `on_time_pct` | one of the registered objectives (see [modules.md](modules.md)) |
| `--optimizer` | `hill_climb` | `grid`, `random`, `hill_climb`, or `genetic` |
| `--budget` | 12 | maximum real (simulated) evaluations |
| `--reps` | 12 | replications per evaluation (the confidence band) |
| `--seed` | 0 | seeds the optimizer's own randomness |

## `twinflow states <run_dir> [--view NAME] [--top K]`

Summarise the moves a run captured. `run_dir` is one run folder, or a folder that holds
replication folders (the `--out` folder of `twinflow scenario run`). The summary shows how
many runs, episodes and moves were found, the time span, the number of different states, the
top states by share of moves (default 10, view `family`), and the money by part (revenue,
production, setup, holding, lost sales, late).

```bash
twinflow scenario run examples/molding-press/molding-press.twin.yaml --reps 2 --out out
twinflow states out --view full --top 5
```

A make-to-stock run writes its moves by itself. A factory (discrete-event) run writes them
only when the capsule has `experiment.state_capture`, for example
`state_capture: {every_hours: 4}`. If no moves are found, the command says so, names the
folder and tells you how to turn capture on (exit 1).

The `family` view needs product facts (families and demand rates). A make-to-stock run
leaves them in `scenario.json` beside its replication folders. When they are missing, as in
a factory run, the command prints a note and uses the `full` view. The views you define in
`model.state_groups` can be named with `--view`.

## `twinflow markov <run_dir> [--view NAME] [--top K] [--min-prob P] [--out DIR]`

Estimate how the plant moves from state to state, print the top states and the likely
moves, and write `markov.json` and `markov.mmd` (a Mermaid chart) into the run folder, or
into `--out`. `--top` is how many states to keep (default 15). `--min-prob` hides arrows
less likely than `P` (default 0.05). The view rules and the fallback note are the same as
for `twinflow states`.

```bash
twinflow markov out --view simple --top 8
```

Exit codes for both commands: 0 ok, 1 a problem you can fix (the message names the path and
the fix), 2 an internal failure.

## `twinflow extensions list|check <project>`

Look at the extension files of a project folder (it holds one `*.twin.yaml` capsule).

`list` prints one row per extension: its kind (policy, reward, view, feature, demand,
observation, engine), its name, its source (`builtin` or `project`), what it overrides, and
how many problems it has. A project file with the name of a built-in replaces it, and the
`overrides` column says so. Add `--json` for a machine-readable list.

`check` runs every extension on sample states and prints each problem in plain words: the
file and line, what is wrong and how to fix it. It exits 1 when any problem is an error,
and 0 otherwise.

```bash
twinflow extensions list examples/molding-press
twinflow extensions check examples/molding-press
```

A folder that does not exist, or has no capsule, gives exit 1 with a message that names the
path and the fix.

## `twinflow policies list|check|race <project>`

Work with the policy files of a make-to-stock project or a custom engine. A policy is a
short Python file that picks what to make next.

`list` shows every policy: its name, where it comes from (`builtin` or `project`), its
decision type, the settings the race can tune, and how many problems it has.

`check` runs the policy files on sample states and prints each problem in plain words.
It leaves out other kinds of extension files, so use `extensions check` for those. It exits
1 when a problem is an error, and 0 otherwise.

`race` runs every policy on the same random demand and prints a ranking:

```bash
twinflow policies race examples/molding-press --episodes 20
```

The table shows rank, policy, score, dollars per run with a 95% range, fill rate and
changeovers per run. Dollars are always shown, so you can see when a policy wins on your
score but loses money. The command prints one progress line for each policy, lists any
policy it had to skip and why, and prints the path of `race.html`.

| Flag | What it does |
|------|--------------|
| `--policies DIR` | Race the files in `DIR` instead of the project's `policies/` folder. Built-in policies still run. |
| `--episodes N` | How many runs each policy gets. The default is the capsule's `experiment.race.episodes` (200). |
| `--seed S` | The seed for the demand draws. |
| `--out DIR` | Where to write `race.json` and `race.html`. The default is `<project>/runs/race-<UTC time>/`. |
| `--observation NAME` | What the policies may see, like `bucketed`. The default is the capsule's `experiment.observation`. |

The race also writes `scenario.json` beside `race.json`, so `twinflow states` and
`twinflow markov` can read the kept moves in `policies/<name>/` with the plant's own views
(for example `--view simple`).

Exit codes for all three: 0 ok, 1 a problem you can fix (a missing folder, a capsule that
is not make-to-stock or custom, a broken file, a bad flag), 2 an internal failure.

## `twinflow improve <project> --candidate FILE [--replace] [--episodes N] [--seed S] [--out DIR]`

Try a new policy file against the ones you have. The candidate and your current policies
play the same random demand. The candidate is copied into `policies/` only when both are true:

- it ranks first, and
- its lead over the previous best is proven. The default rule is that the 95% range of the
  paired difference in score is above zero. The capsule can set `experiment.race.promote_rule`
  to `mean_above_zero` to ask only that the average is above zero.

```bash
twinflow improve examples/molding-press --candidate my_rule.py
```

The command prints the score and dollars of both policies and the difference between them.
When the candidate wins, it prints where the file went. When it does not, it prints why and
copies nothing. Either way the exit code is 0. A file that does not exist, a policy that
fails to load or breaks, or a project with no policies gives exit 1.

- If `policies/` already has a file with the same name, the command stops with exit 1.
  Add `--replace` to overwrite it when the candidate wins. The old copy is not in the race.
- If your project has no policy files, the candidate is raced against the built-in policies.
- `improve.json` (in `--out`, default `<project>/runs/improve-<UTC time>/`) holds the proof:
  both policies, their settings, both ranges, the seeds and the reason. `race.json` and
  `race.html` are written next to it.

## `twinflow new <folder> --template NAME` and `twinflow new --list`

Start a new project from a template. `--list` shows the templates:

| Template | What you get |
|----------|--------------|
| `make-to-stock` | The molding press from `examples/molding-press`, with two policy files. |
| `job-shop` | The CNC shop with state capture and money turned on, and one dispatch rule file. |
| `custom-engine` | A small Python world (a bakery) in `engines/`, a capsule, one policy and a README. |

```bash
twinflow new my-plant --template make-to-stock
twinflow extensions check my-plant
twinflow policies race my-plant --episodes 50
```

The capsule in the new folder is named after the folder (`my-plant.twin.yaml`). Every
template has a README in plain words. The command stops with exit 1 when the folder is not
empty, and changes nothing. The `job-shop` template is a discrete-event twin: run it with
`twinflow scenario run`, then read it with `twinflow states`. `policies race` and
`extensions check` are for make-to-stock and custom projects.

## `twinflow serve [--host --port --models-root]`

Start the local REST API (needs the `api` extra). Defaults to `127.0.0.1:8000` and
serves the example floors under `examples/`.

```bash
twinflow serve --port 8000
```

See [api.md](api.md) for the endpoints and [frontend.md](frontend.md) for the UI.

The workspace CLI also provides scenario capsule, snapshot/data, and verified scheduling
commands, plus administration commands for local workspace operations. Their supported
flows and boundaries are documented in the [workspace guide](enterprise/README.md) and
[agent integration guide](enterprise/AGENT-GUIDE.md); they use workspace contracts,
separate from the legacy `model.yaml` simulation commands above.
