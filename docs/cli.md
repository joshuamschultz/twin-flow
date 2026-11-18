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
