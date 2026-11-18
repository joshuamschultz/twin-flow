# %% [markdown]
# # For analysts: Notebook 2: Does the state hold enough to decide?
#
# *This notebook is for analysts. It uses twinflow's Python API. A planner who wants the
# commands and the decision should start with* `examples/showcase/plastics-molding/`.
#
# **The plant.** The same one as in Notebook 1: a molding press, many products in a few color
# families,
# one product per day, and costly changeovers. This notebook stands on its own.
#
# **The lesson.** A good state holds everything needed to pick a good action.
# This is the **Markov property**.
# Here we hide parts of the state from a rule and see what it costs.
# We also find two different states that look the same to a blurry view but need different actions.
#
# **The tool.** In twinflow a hidden part is an **observation mask**.
# A mask is a small file with one function, `observe(state)`. It turns the full state into what
# the rule sees.
# The rule itself does not change. The capsule setting `experiment.observation` lists the masks to
# race.
#
# **Fast mode.** Set the environment variable `TWINFLOW_FAST=1` to run a small version in seconds.
# The charts and tables are the same. The numbers are rougher.

# %%
import os
import shutil
import tempfile
import time
from pathlib import Path

import plotly.graph_objects as go
import polars as pl

from twinflow.extensions import analysis
from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import race
from twinflow.report import theme
from twinflow.state.records import Snapshot

FAST = os.environ.get("TWINFLOW_FAST") == "1"
EPISODES = 10 if FAST else 150  # runs per rule and view in the race
TUNE_EPISODES = 3 if FAST else 30  # runs per setting when a rule is tuned
print(
    "fast mode" if FAST else "full mode",
    "| runs per rule and view:",
    EPISODES,
    "| runs per tuning try:",
    TUNE_EPISODES,
)

# Every chart draws with the twinflow design tokens, the same colors and fonts as the report pages.
theme.apply()

pl.Config.set_tbl_rows(40)
pl.Config.set_tbl_cols(20)
pl.Config.set_fmt_str_lengths(200)


def find_example() -> Path:
    """The molding-press folder, wherever this notebook was started from."""
    for folder in [Path.cwd(), *Path.cwd().parents]:
        for candidate in (folder, folder / "examples" / "molding-press"):
            if (candidate / "molding-press.twin.yaml").exists():
                return candidate
    raise FileNotFoundError("Start this notebook inside examples/molding-press/notebooks/analyst.")


def rounded(table: pl.DataFrame, digits: int = 1) -> pl.DataFrame:
    """The same table with every decimal number rounded, so it is easy to read."""
    return table.with_columns(pl.col(pl.Float64).round(digits))


# %% [markdown]
# ## Step 1. The problem definition
#
# The plant is the capsule file. We copy it to a work folder and open that folder as a project.
# The two policy files in the example folder are left out, so only the built-in rules race.
# That keeps the numbers equal to the research repo.
# Notebook 1 explains the plant in full. Here we only print what we need.

# %%
EXAMPLE = find_example()
WORK = Path(tempfile.mkdtemp(prefix="twinflow-press-"))
shutil.copy(EXAMPLE / "molding-press.twin.yaml", WORK)

project = Project.open(WORK)
facts = project.facts
model = project.capsule.model
print("project is ready:", project.ok, "| problems:", len(project.problems))
print(
    f"{len(facts.products)} products in {len(set(facts.family.values()))} families: "
    f"{', '.join(sorted(set(facts.family.values())))}"
)
print(
    f"a run lasts {model['horizon']} days; each product starts with {model['start_cover_days']} "
    "days of stock"
)
print("the capsule picks these masks by default:", ", ".join(project.config.observations))

# %% [markdown]
# ## Step 2. The state
#
# A state is the day, the stock of every product, and which product the press is set up for.
# The engine gives it out as a snapshot. A rule gets a **policy state**, which adds helpers like
# days of cover.

# %%
engine = project.engine()
start = engine.start(0)
state = project.policy_state(start)
print(
    f"day {start.period}, press is set up for {start.setup[facts.line]}, days left: "
    f"{state.days_left}"
)
rounded(
    pl.DataFrame(
        {
            "product": list(facts.products),
            "stock (units)": [start.stock[p] for p in facts.products],
            "days of cover": [state.days_of_cover[p] for p in facts.products],
        }
    )
)

# %% [markdown]
# ## Step 2b. Observation masks: what the planner is allowed to see
#
# The state holds everything. But a planner may see only part of it.
# Maybe the numbers are blurry, or one piece is missing.
# An **observation mask** turns the full state into what the planner sees.
# Then we run the same rule on the blurred view and see what it costs.
#
# These are the mask files twinflow ships with.

# %%
masks = project.extensions.of("observation")
pl.DataFrame(
    {
        "mask": [m.name for m in masks],
        "what the rule sees": [m.summary for m in masks],
        "file": [m.file.name for m in masks],
    }
)

# %% [markdown]
# A mask is a short file. Here is the one that squashes stock into low, medium and high.
# A hidden part is replaced with a neutral guess. The rule itself is not changed.

# %%
print(project.extensions.get("observation", "bucketed").file.read_text())

# %% [markdown]
# **Check it.** One example state. What does the planner see under each view?
# Masks joined with `+` apply left to right. The last mask here joins two.

# %%
MASKS = ["full", "no_setup", "bucketed", "own_product", "no_days_left", "bucketed+no_days_left"]
example = Snapshot(
    t=20.0,
    period=20,
    stock=dict(zip(facts.products, [300, 40, 20, 10, 100, 30, 12, 6, 40, 30, 10, 5], strict=True)),
    setup={facts.line: "GRY-L"},
)
seen = {mask: project.observe(project.policy_state(example), mask) for mask in MASKS}
print(f"Full state: day {example.period}, press on {example.setup[facts.line]}")
rounded(
    pl.DataFrame(
        {
            "product": list(facts.products),
            **{mask: [float(s.stock[p]) for p in facts.products] for mask, s in seen.items()},
        }
    )
)

# %%
pl.DataFrame(
    {
        "mask": MASKS,
        "day seen": [s.period for s in seen.values()],
        "press seen on": [s.setup for s in seen.values()],
    }
)

# %% [markdown]
# Two helpers give a **name to each view**, so we can count visits and draw maps later.
# - `bucketed` names every product low, medium or high, plus the product on the press.
# - `family` is a smaller view for maps. It uses only each family's thinnest product, plus the
#   family on the press.
#
# `analysis.analysis_view` returns a function from a snapshot to its name.

# %%
bucketed_name = analysis.analysis_view(project, "bucketed")
family_name = analysis.analysis_view(project, "family")
print("per-product view of the example:", bucketed_name(example))
print("family view of the example:", family_name(example))

# %% [markdown]
# ## Step 3. The transition function
#
# The transition says how today becomes tomorrow. One call does it: `engine.step(action)`.
# It returns a move: the state before, the action, the events of the day and the state after.
# The engine steps the real state. A mask only changes what the rule sees. It never changes the
# plant.

# %%
engine = project.engine(project.demand_draws(episodes=1, seed=2024))
engine.start(0)
move = engine.step("GRY-S")
sold = sum(v for k, v in move.events.items() if k.startswith("sold."))
lost = sum(v for k, v in move.events.items() if k.startswith("lost."))
print(f"run {move.action}: made {move.events['made.GRY-S']:g} units, sold {sold:g}, lost {lost:g}")
print(f"press was on {move.before.setup[facts.line]}, and is now on {move.after.setup[facts.line]}")

# %% [markdown]
# ## Step 4. The reward function
#
# The reward is the money we make or lose in one day. It is in dollars.
# `revenue - production - setup - holding - lost sales`
# Each move carries this split in its ledger.

# %%
ledger = move.ledger
rounded(
    pl.DataFrame(
        {
            "part": ["revenue", "production", "setup", "holding", "lost_sales", "late", "total"],
            "dollars": [
                ledger.revenue,
                ledger.production,
                ledger.setup,
                ledger.holding,
                ledger.lost_sales,
                ledger.late,
                ledger.total,
            ],
        }
    )
)

# %% [markdown]
# ## Step 5. Policies
#
# We race three rules that read the state in different ways:
# - **lowest_cover** looks at stock only.
# - **family_campaign** looks at stock, and at which color is on the press.
# - **s_big_s** looks at stock and at the day. It stops building stock near the end of the run.
#
# Here is the third one. Read how it uses `state.days_left`.

# %%
RULES = ["s_big_s", "family_campaign", "lowest_cover"]
print(project.extensions.get("policy", "s_big_s").file.read_text())

# %% [markdown]
# Each rule has a `SETTINGS` grid. The race tunes it on separate demand, under the full state.
# Then it uses those settings under every view. So a rule is not re-tuned for a blurry view.
# That is how the research repo did it, and it is the question we ask:
# what does the same rule lose when we hide something from it?

# %% [markdown]
# ## Step 6. The simulation: race every rule under every view
#
# `race(project, policies, observations=[...])` runs each rule under each mask.
# Every run in every view uses the same random demand.

# %%
project = Project.open(WORK)
settings = RaceSettings(episodes=EPISODES, seed=11, tune_episodes=TUNE_EPISODES, tune_seed=777)
started = time.time()
result = race(project, RULES, settings=settings, observations=MASKS)
print(f"raced {len(RULES)} rules under {len(MASKS)} views in {time.time() - started:.1f} seconds")
print("rules skipped because of a problem:", len(result.skipped))
tuned = {row.policy: dict(row.settings) for row in result.ranking()}
for name, values in tuned.items():
    print(f"  {name}: {values}")

# %% [markdown]
# ## Step 7. Analysis and charts
#
# ### Profit by view
# Mean profit per run of each rule under each view, on the same random demand.
# The second table shows the money lost against the full state.
# A negative loss means the rule did better with less information.

# %%
by_view = analysis.profit_by_view(result)
rounded(by_view)

# %%
rounded(analysis.profit_by_view(result, loss=True))

# %%
fig = go.Figure()
for name in RULES:
    fig.add_bar(name=name, x=by_view["observation"], y=by_view[name])
fig.update_layout(barmode="group", yaxis_title="mean profit per run ($)", height=500)
theme.question(
    fig,
    "Does a rule earn less when it sees less?",
    "Compare each bar with the full view. A shorter bar is money lost to what the rule could not "
    "see.",
)

# %% [markdown]
# Why do some rules lose so much, and one rule gain?
# Look at what the rules did under the full view and the bucketed view.

# %%
rows = []
for name in ("s_big_s", "lowest_cover"):
    for mask in ("full", "bucketed"):
        row = result.result(name, mask)
        rows.append(
            {
                "rule": name,
                "view": mask,
                "mean profit ($)": row.dollars.mean,
                "fill rate (%)": None if row.fill_rate is None else 100 * row.fill_rate,
                "changeovers / run": row.changeovers,
                "cross-family / run": row.cross_family,
                "idle days (%)": 100 * row.idle_share,
            }
        )
rounded(pl.DataFrame(rows))

# %% [markdown]
# ### State aliasing: two different states that look the same
# Two full states can fall into the same bucket. A rule that sees only the bucket must treat them
# the same.
# But their best actions can differ.
# To find "best", we try each action once, then follow the rule for a few days on the same random
# demand.
# We compare the average money.

# %%
MAP_RUNS = 30 if FAST else 300
budget = {"buckets": 2, "per_bucket": 3, "draws": 10, "lookahead": 10} if FAST else {}
s_big_s_runs = analysis.run_episodes(
    project, "s_big_s", MAP_RUNS, seed=5, settings=tuned["s_big_s"]
)
pair = analysis.find_aliased_pair(
    project, s_big_s_runs, "s_big_s", settings=tuned["s_big_s"], view="bucketed", **budget
)
if pair is None:
    raise RuntimeError("no aliased pair was found; use more runs")
analysis.aliasing_table(project, pair)

# %%
first, second = pair.states
print(
    f"Regret of mixing the two states up: ${pair.regret:,.0f} over {pair.lookahead} days (sum for "
    "both states)"
)
for label, state_ in (("A", first), ("B", second)):
    cover = [round(state_.stock[p] / facts.demand_rate[p], 1) for p in facts.products]
    print(f"State {label}: days of cover", cover)

# %% [markdown]
# Read the table.
# - Both states have the same label (see the row "same view"). A bucketed rule cannot tell them
#   apart.
# - Their best actions differ (row "best action").
# - Using the other state's best action loses money over the look-ahead.
#   Compare the "dollars (best)" row with the "dollars (other)" row.
# - Look at the one-day dollars too. They can point to the wrong action.
#   A good result today is not a good result over many days.

# %% [markdown]
# ### How predictable is tomorrow?
# A good state makes tomorrow predictable.
# We measure the spread (entropy, in bits) of tomorrow's family view. More bits means more spread,
# so the state tells you less.
# For common pairs of (today's family view, action) we compare two views of today:
# - **Grouped view:** we know only the family view and the action.
# - **Full view:** we know the exact stock, setup and action.

# %%
budget = {"min_rows": 20, "max_groups": 8, "states_per_group": 5, "draws": 30} if FAST else {}
entropy = analysis.next_state_entropy(project, s_big_s_runs, view="family", **budget)
share = (entropy["grouped view"] >= entropy["full view"]).mean()
print(f"{share:.0%} of pairs: grouped spread >= full spread")
print({name: round(float(entropy[name].mean()), 2) for name in ("grouped view", "full view")})

fig = go.Figure()
pairs = [
    f"{label} / {action}" for label, action in zip(entropy["label"], entropy["action"], strict=True)
]
fig.add_bar(name="grouped view (family view + action)", x=pairs, y=entropy["grouped view"])
fig.add_bar(name="full view (exact state + action)", x=pairs, y=entropy["full view"])
fig.update_layout(
    barmode="group", yaxis_title="spread of tomorrow's family view (bits)", height=550
)
theme.question(
    fig,
    "Does the exact state tell us more about tomorrow?",
    "A taller bar means tomorrow is harder to predict.",
)

# %% [markdown]
# The exact stock narrows tomorrow's bucket. The grouped view is blurrier.
# Part of what looks like random luck in the grouped view is really detail we hid.

# %% [markdown]
# ### Adding the missing piece back: the day
# The "no_days_left" mask cannot see that the plan is ending.
# On the last days, extra stock is worth nothing, because we cannot sell it after the horizon.
# A rule that sees the day stops building stock. One that cannot see it keeps spending.
#
# First, three single states. Then the effect of putting the day back over whole runs.

# %%
DRAWS = 20 if FAST else 200
rule = project.extensions.get("policy", "s_big_s")
engine = project.engine()
three_days = {p: min(facts.max_storage[p], round(3 * facts.demand_rate[p])) for p in facts.products}
rows = []
for day in (59, 58, 30):
    here = Snapshot(
        t=float(day), period=day, stock=three_days, setup={facts.line: facts.products[0]}
    )
    for label, mask in (("sees the day", "full"), ("cannot see the day", "no_days_left")):
        decide = project.decider(rule, engine, settings=tuned["s_big_s"], observation=mask)
        action = decide(here)
        value = analysis.action_values(
            project,
            here,
            "s_big_s",
            settings=tuned["s_big_s"],
            lookahead=model["horizon"] - day,
            draws=DRAWS,
            seed=1,
        )[action]
        rows.append(
            {"day": day, "rule": label, "action": action, "money to end of plan ($)": value}
        )
rounded(pl.DataFrame(rows))

# %%
profit = result.profit_by_view()
back = {
    "full state: day hidden -> day back": ("no_days_left", "full"),
    "bucketed stock: day hidden -> day back": ("bucketed+no_days_left", "bucketed"),
}
rounded(
    pl.DataFrame(
        {
            "rule": RULES,
            **{
                label: [profit[name][after] - profit[name][before] for name in RULES]
                for label, (before, after) in back.items()
            },
        }
    )
)

# %% [markdown]
# ### Markov chain maps: full view and bucketed view, side by side
# Under the (s,S) rule we record where the plant goes each day and count the moves between states.
# - **Full view:** the exact stock of all products plus the press setting. The number of possible
#   states is huge.
# - **Bucketed view (family level):** each family's thinnest product low, medium or high, plus the
#   family on the press.
#   Only a few dozen states.
#
# We show the most visited states of each.

# %%
moves = [m for run in s_big_s_runs for m in run]
chains = {
    "full": project.markov(moves, "full", top_k=15),
    "bucketed (family)": project.markov(moves, "family", top_k=15),
}
for name, chain in chains.items():
    print(
        f"{name:18s}: {len(chain.share):,} different states in {chain.moves:,} days; top 15 cover "
        f"{chain.coverage:.1%} of all days"
    )
theme.question(
    analysis.markov_heatmap(chains),
    "Where does the plant go next, in each view?",
    "Each row is where we are. Each column is where we go. A bright cell is a likely move.",
)

# %% [markdown]
# Rows do not add to 1 because tomorrow can be a state outside the top states.
# In the **full** view the plant almost never lands in the same exact state twice.
# Nearly every day is a state seen once.
# So the top states are just the shared start state plus arbitrary one-visit states.
# The bright diagonal is only the first run's path, one step after another. It is not a pattern.
# The map says nothing about how the plant moves.
# In the **bucketed** view the plant revisits the same states again and again, so the map shows
# real structure.

# %%
theme.question(
    analysis.markov_network(chains),
    "How does each view group the plant's days?",
    "Big dots are common states. Thick arrows are likely moves.",
)

# %% [markdown]
# ### Stock by product by day, one run
# Top: days of cover for every product (the full view).
# Bottom: the same, squashed to low, medium and high (the bucketed view).

# %%
theme.question(
    analysis.inventory_grid(project, s_big_s_runs[0]),
    "Which products run thin, and when?",
    "Orange is low stock. Dashed lines are the days the press changed product.",
)

# %% [markdown]
# # Conclusion
#
# **The short version.** A rule is only as good as the state it sees.
# Hide part of the state and the same rule usually makes less money.
# The exceptions are worth knowing.
#
# All numbers below are mean profit per run, on the same random demand for every view.
# The rules are the tuned versions from the race.
#
# **What to look for in the table.**
# - Seeing only your own product is the worst view for every rule. The press cannot see the other
#   shelves emptying.
# - Which product the press is on matters to the rules that use it. A rule that does not use it
#   loses nothing.
# - The day matters to rules that stop building stock near the end.
# - Blurry stock can hurt a rule a lot, and can even help another. A blurry view is not always
#   worse.
# - State aliasing is the reason. Two different states look the same, but need different actions.
# - Hidden detail looks like luck. The exact state predicts tomorrow better than the blurry one.
#
# **What this does not show.** We hid information from rules that were tuned with full information.
# A rule tuned for the blurry view might do better. And these are hand-made rules, not learned ones.

# %%
table = by_view.to_dicts()
print("Mean profit per run by view ($):")
print(f"{'view':24s}" + "".join(f"{name:>18s}" for name in RULES))
for row in table:
    print(f"{row['observation']:24s}" + "".join(f"{row[name]:>18,.1f}" for name in RULES))
print()
losses = analysis.profit_by_view(result, loss=True).to_dicts()
worst = max(
    ((row[name], name, row["observation"]) for row in losses for name in RULES), key=lambda t: t[0]
)
print(f"Biggest loss against the full state: ${worst[0]:,.1f}, rule {worst[1]}, view {worst[2]}")
gains = [
    (-row[name], name, row["observation"])
    for row in losses
    for name in RULES
    if row[name] is not None and row[name] < 0
]
print(
    "Rules that did better with less information:",
    "; ".join(f"{n} under {o} (+${g:,.1f})" for g, n, o in gains) or "none",
)
print(
    f"Aliased pair: both look like '{pair.bucket}'. Best actions {pair.best[0]} and "
    f"{pair.best[1]}. Regret ${pair.regret:,.0f}."
)
print(
    f"Family view top 15 covers {chains['bucketed (family)'].coverage:.1%} of days; full view top "
    f"15 covers {chains['full'].coverage:.1%}."
)
