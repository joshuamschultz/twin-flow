# Molding press notebooks

Two notebooks rebuild the research repo's lessons on top of twinflow.
They use only twinflow's public API. The plant comes from `../molding-press.twin.yaml`.

| Notebook | The question | What it shows |
|---|---|---|
| `01_long_term_policy.ipynb` | Is a good rule today a good rule later? | The capsule, the state, one engine step and its ledger, the built-in rule files, a race of every rule, profit per rule, where the money goes, every tuning try, profit over time with bands, who leads early and late, plan length, a Markov map of the winner, stock by product by day. The last part shows how a company adds its own rule, reward and settings grid. |
| `02_state_information.ipynb` | Does the state hold enough to decide? | Observation masks, profit by view, two different states that look the same, how much a view says about tomorrow, adding the day back, full and bucketed Markov maps, stock by product by day. |

## Run them

Jupyter is not part of twinflow. Start it for one session with `uv`:

```bash
cd examples/molding-press/notebooks
uv run --with jupyterlab jupyter lab
```

Then open a notebook and choose **Run All**. Start Jupyter in this folder, so the notebook can find the capsule.

A full run takes under a minute for each notebook.

## Fast mode

Set `TWINFLOW_FAST=1` to run a small version in a few seconds.
The tables and charts are the same. The numbers are rougher, because there are fewer runs.

```bash
TWINFLOW_FAST=1 uv run --with jupyterlab jupyter lab
```

## How they are made

The notebooks are built from short Python files in `src/`.
A line `# %% [markdown]` starts a text cell. A line `# %%` starts a code cell.
The `.ipynb` files are saved with no outputs, so a change shows up as a small diff.

Edit a file in `src/`, then build the notebooks again:

```bash
uv run python examples/molding-press/notebooks/build_notebooks.py
```

The test `tests/integration/test_molding_press_notebooks.py` fails when a notebook does not match its source.
It also runs every code cell of both notebooks in fast mode.
