# %% [markdown]
# # For analysts: Notebook 1: A good rule today may be a bad rule later
#
# *This notebook is for analysts. It uses twinflow's Python API. A planner who wants the
# commands and the decision should start with* `examples/showcase/plastics-molding/`.
#
# **The plant.** One molding press makes housings for a dozen products. We build to stock.
# Each day the planner picks one product to run, or lets the press rest.
# A switch to a new product costs money and output.
# A switch to a new color costs far more than a switch to a new size.
#
# **The lesson.** A rule can look good early in the run and look bad at the end.
# We tune every rule in the same fair way. Then we race them all on the same random demand.
# The charts at the end show who leads when, and why.
#
# **The tool.** This notebook uses only the public twinflow API.
# The plant, the demand and the money rules come from one capsule file.
# Each decision rule is a short Python file with one function: `decide(state)`.
#
# **Fast mode.** Set the environment variable `TWINFLOW_FAST=1` to run a small version in seconds.
# The charts and tables are the same. The numbers are rougher.
#
# Notebook 2 asks a second question: what must the state tell the planner?

# %%
import dataclasses
import os
import shutil
import tempfile
import textwrap
import time
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import polars as pl
from plotly.subplots import make_subplots

from twinflow.extensions import analysis
from twinflow.extensions.project import Project, RaceSettings
from twinflow.extensions.race import race
from twinflow.extensions.report import write_race_report
from twinflow.extensions.report_model import race_view_model
from twinflow.report import decision, theme

FAST = os.environ.get("TWINFLOW_FAST") == "1"
EPISODES = 10 if FAST else 200  # runs per rule in the race
TUNE_EPISODES = 3 if FAST else 30  # runs per setting when a rule is tuned
print("fast mode" if FAST else "full mode", "| runs per rule:", EPISODES, "| runs per tuning try:", TUNE_EPISODES)

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


# Every chart in this notebook draws with the twinflow design tokens: the same colors and fonts
# as the report pages. `theme.apply()` sets them as the Plotly default.
theme.apply()


# %% [markdown]
# ## Step 1. The problem definition
#
# In the research repo this step was a Python class with many named numbers.
# In twinflow the problem is one **capsule** file. It holds the plant, the money rules,
# the assumptions and where the numbers came from.
# We copy the capsule to a work folder. Then we open that folder as a **project**.
#
# The example folder also holds two policy files of its own, written for the README.
# We leave them out here. We want to race the built-in rules, so the numbers can be
# checked against the research repo.

# %%
EXAMPLE = find_example()
WORK = Path(tempfile.mkdtemp(prefix="twinflow-press-"))
shutil.copy(EXAMPLE / "molding-press.twin.yaml", WORK)

project = Project.open(WORK)
print("capsule file:", project.capsule_file.name)
print("project is ready:", project.ok, "| problems:", len(project.problems))
print()
capsule_text = (WORK / "molding-press.twin.yaml").read_text()
print("\n".join(line for line in capsule_text.splitlines() if not line.lstrip().startswith("- {id:")))

# %% [markdown]
# The capsule has four parts we care about:
# - **model**: the plant. The press, the products, the changeover costs and the holding and lost-sale rates.
# - **experiment**: how to run and race it. Here it holds the seed and the default rule.
# - **assumptions**: what the model takes as true.
# - **provenance**: where the numbers came from.
#
# First the plant-wide numbers. They are the same for every product.

# %%
model = project.capsule.model
changeover = model["line"]["changeover"]
economics = model["economics"]["defaults"]
rows = [
    ("horizon (days in one run)", model["horizon"], "How many days we plan."),
    ("start_cover_days", model["start_cover_days"], "Days of average sales on the shelf on day 0."),
    ("within-family changeover cost ($)", changeover["within_family"]["cost"], "Same color, new size."),
    ("within-family output lost", changeover["within_family"]["output_lost"], "Share of the day's output lost."),
    ("across-family changeover cost ($)", changeover["across_family"]["cost"], "New color. The press must be purged."),
    ("across-family output lost", changeover["across_family"]["output_lost"], "Share of the day's output lost."),
    ("holding rate (per night)", economics["holding_rate"], "Share of the unit cost, for each night a unit sits."),
    ("lost-sale rate", economics["lost_sale_rate"], "Share of the price, charged on top of the missed sale."),
]
pl.DataFrame([(name, f"{value:g}", meaning) for name, value, meaning in rows], schema=["setting", "value", "meaning"], orient="row")

# %% [markdown]
# The per-product numbers go in one table. There is one row per product.
# A product is one item we stock: one color in one size.
# - **abc**: A is a fast seller. B is medium. C is a slow mover.
# - **demand**: average units sold per day. The real number is random (a Poisson draw).
# - **run_rate**: units the press makes in one full day.
# - **price** and **unit_cost**: dollars per unit.
# - **max_storage**: the most units the rack can hold.

# %%
products = pl.DataFrame(model["products"])
total_demand = products["demand"].sum()
rounded(products.with_columns((100 * pl.col("demand") / total_demand).alias("share of demand %")))

# %% [markdown]
# A quick check of the shape of demand. A few products should carry most of it.

# %%
rounded(
    products.group_by("abc")
    .agg(pl.len().alias("products"), pl.col("demand").sum().alias("demand"))
    .with_columns((100 * pl.col("demand") / total_demand).alias("share of demand %"))
    .sort("abc")
)

# %% [markdown]
# How busy is the press? For each product, demand divided by run rate is the share of a day we must run it.
# The sum should be high, but below 100%, so the choices matter.

# %%
facts = project.facts
busy = sum(facts.demand_rate[p] / facts.run_rate[p] for p in facts.products)
lost = changeover["within_family"]["output_lost"]
with_changeovers = sum(facts.demand_rate[p] / (facts.run_rate[p] * (1 - 0.5 * lost)) for p in facts.products)
print(f"products: {len(facts.products)}, actions: {len(project.actions)} (every product, plus idle)")
print(f"Press busy time needed: {busy:.0%} of days (about {with_changeovers:.0%} once changeovers cost output)")
print()
print("The capsule says the model takes these as true:")
for line in project.capsule.assumptions:
    print(" -", line)
print("Where the numbers came from:", project.capsule.provenance["source"])

# %% [markdown]
# ## Step 2. The state
#
# A **state** is everything we know at the start of a day.
# Here it is the day, the stock of every product, and which product the press is set up for.
# The engine hands it out as a **snapshot**. We start an engine and look at day 0.

# %%
engine = project.engine()
start = engine.start(0)
print(f"day {start.period}, press '{facts.line}' is set up for {start.setup[facts.line]}")
rounded(
    pl.DataFrame(
        {
            "product": list(facts.products),
            "family": [facts.family[p] for p in facts.products],
            "stock (units)": [start.stock[p] for p in facts.products],
            "days of cover": [start.stock[p] / facts.demand_rate[p] for p in facts.products],
        }
    )
)

# %% [markdown]
# A policy file does not get the raw snapshot. It gets a **policy state**.
# This is the same facts, with helpers added: days of cover, days left, family of each product and more.
# These are the names a `decide(state)` function can use.

# %%
state = project.policy_state(start)
fields = [name for name in dir(state) if not name.startswith("_") and name != "replace"]
print("a policy state offers:", ", ".join(fields))
print("days left in the run:", state.days_left)
print("actions to choose from:", ", ".join(state.actions))

# %% [markdown]
# ## Step 3. The transition and the ledger
#
# The **transition** says how today becomes tomorrow. Given the state, our action and the random demand:
# 1. If we run a product, the press makes units. A changeover costs part of the day. Output cannot pass the storage limit.
# 2. Customers buy what we have. Demand we cannot meet is lost.
# 3. The day goes up by one. The press stays set up for the product we ran.
#
# In twinflow one call does it: `engine.step(action)`. It returns a **move**.
# A move holds the state before, the action, the events of the day, the state after and the money ledger.
#
# To see one day by hand, we draw the demand ourselves with numpy. Then we hand it to the engine.

# %%
rng = np.random.default_rng(0)
rng.poisson(facts.demand_rate["GRY-S"])  # numpy spends one draw on a one-product plant first
one_day = rng.poisson([facts.demand_rate[p] for p in facts.products])
draws = np.zeros((1, model["horizon"], len(facts.products)), dtype=np.int64)
draws[0, 0] = one_day

engine = project.engine(draws)
before = engine.start(0)
move = engine.step("GRY-S")  # run the first product
events = move.events
rounded(
    pl.DataFrame(
        {
            "product": list(facts.products),
            "stock before": [move.before.stock[p] for p in facts.products],
            "made": [events[f"made.{p}"] for p in facts.products],
            "demand": [events[f"demand.{p}"] for p in facts.products],
            "sold": [events[f"sold.{p}"] for p in facts.products],
            "lost": [events[f"lost.{p}"] for p in facts.products],
            "stock after": [move.after.stock[p] for p in facts.products],
        }
    )
)

# %% [markdown]
# **Add changeovers.** We run the first product again. This time we start the press set up for other products.
# The same color means a small output loss. A new color means a big one.

# %%
CASES = [
    ("press already on GRY-S (no changeover)", "GRY-S"),
    ("press on GRY-M (same family, within)", "GRY-M"),
    ("press on BLK-S (other family, across)", "BLK-S"),
]


def one_day_from(setup_on, action="GRY-S"):
    """One day of demand from `one_day`, starting with the press set up for `setup_on`."""
    engine = project.engine(draws)
    state = dataclasses.replace(engine.start(0), setup={facts.line: setup_on})
    engine.start(0, from_state=state)
    return engine.step(action)


moves = {label: one_day_from(setup_on) for label, setup_on in CASES}
rounded(
    pl.DataFrame(
        {
            "case": list(moves),
            "units of GRY-S made": [m.events["made.GRY-S"] for m in moves.values()],
            "share of a full day": [m.events["made.GRY-S"] / facts.run_rate["GRY-S"] for m in moves.values()],
        }
    ),
    2,
)

# %% [markdown]
# ## Step 4. The reward function
#
# The **reward** is the money we make or lose in one day. It is in dollars, with no made-up scores.
#
# `reward = revenue - production - setup - holding - lost sales`
#
# - **revenue** is the price times the units sold.
# - **production** is the unit cost times the units made.
# - **setup** is the changeover cost. It is zero if we stay on the same product or rest.
# - **holding** is the holding cost for each unit left on the shelf tonight.
# - **lost sales** is the penalty for each unit of demand we could not meet.
#
# Each move carries this split in `move.ledger`.
# The race ranks rules by a **reward file**. The default one is `dollars`. We print its source below.

# %%
ledger_parts = ["revenue", "production", "setup", "holding", "lost_sales", "late"]
ledgers = {label: m.ledger for label, m in moves.items()}
rounded(
    pl.DataFrame(
        {
            "case": list(ledgers),
            **{part: [getattr(ledger, part) for ledger in ledgers.values()] for part in ledger_parts},
            "total": [ledger.total for ledger in ledgers.values()],
        }
    )
)

# %% [markdown]
# Look at the totals. The across-family plan has the highest one-day reward.
# It lost half its output, so it made fewer units and paid less production cost.
# But fewer units made means thinner shelves later. One day of reward does not show that.
# Keep this in mind for the rest of the notebook.
#
# Check one total by hand for the first plan. Then print the reward file.

# %%
ledger = ledgers[CASES[0][0]]
print(
    f"{ledger.revenue:,.1f} - {ledger.production:,.1f} - {ledger.setup:,.1f} - "
    f"{ledger.holding:,.1f} - {ledger.lost_sales:,.1f} - {ledger.late:,.1f} = "
    f"{ledger.revenue - ledger.production - ledger.setup - ledger.holding - ledger.lost_sales - ledger.late:,.1f}"
)
print(f"ledger.total = {ledger.total:,.1f}")
print()
print("reward files:", ", ".join(project.extensions.names("reward")))
print()
print(project.extensions.get("reward", "dollars").file.read_text())

# %% [markdown]
# ## Step 5. Policies
#
# A **policy** is a rule: "given the state, pick an action." Real planners use rules like these.
# In twinflow each rule is one Python file with one function, `decide(state)`.
# Twinflow ships with built-in rules. A company can add its own (see the last section).
#
# Here is every policy file the project can use.
# `settings tried` is the grid in the file's `SETTINGS`. The race tries each value to find the best one.

# %%
policy_files = project.extensions.of("policy")
pl.DataFrame(
    {
        "policy": [p.name for p in policy_files],
        "source": [p.source for p in policy_files],
        "what it does": [p.summary for p in policy_files],
        "settings grid": [", ".join(f"{k}: {len(v)} values" for k, v in p.grid.items()) or "none" for p in policy_files],
        "file": [p.file.name for p in policy_files],
    }
)

# %% [markdown]
# Open one file. This is the rule that stays with one color until that color is stocked up.
# Read `decide`. It is plain Python. It takes the state and returns the name of a product, or `"idle"`.

# %%
campaign = project.extensions.get("policy", "family_campaign")
print(campaign.file.read_text())

# %% [markdown]
# A policy is just a function. We can call it by hand on the day-0 state.
# The extension object passes its settings for us.

# %%
for target_days in (6, 14, 30):
    print(f"family_campaign with target_days={target_days}:", campaign(state, target_days=target_days))
print("lowest_cover:", project.extensions.get("policy", "lowest_cover")(state))

# %% [markdown]
# **A quick look.** We run every rule with its default setting on a few random runs of the same demand (the code names the number).
# This is only a first look. The fair race comes next.

# %%
QUICK_RUNS = 10


def mean_profit(runs):
    """Mean dollars per run, for runs from `analysis.run_episodes`."""
    return float(np.mean([sum(m.ledger.total for m in run) for run in runs]))


quick = {name: mean_profit(analysis.run_episodes(project, name, QUICK_RUNS, seed=1)) for name in project.extensions.names("policy")}
rounded(pl.DataFrame({"policy": list(quick), "mean profit ($)": list(quick.values())}).sort("mean profit ($)", descending=True))

# %% [markdown]
# **Tune each rule fairly.** Each rule has settings, like target days.
# We must not give one rule a better setting than another.
# So every rule gets the same tuning budget, and it is tried on separate random demand.
# The final results use different demand. The race does the tuning for us.

# %% [markdown]
# ## Step 6. The simulation
#
# Now we run whole episodes. `analysis.run_episodes` returns one list of moves per run.
# Each move is one day: the state, the action, the demand and the money.
# The same seed gives the same demand to every rule. This is called common random numbers.

# %%
runs_a = analysis.run_episodes(project, "family_campaign", 2, seed=3, settings={"target_days": 14.0})
runs_b = analysis.run_episodes(project, "lowest_cover", 2, seed=3)
demand_a = [{k: v for k, v in m.events.items() if k.startswith("demand.")} for run in runs_a for m in run]
demand_b = [{k: v for k, v in m.events.items() if k.startswith("demand.")} for run in runs_b for m in run]
print("Same demand for both rules:", demand_a == demand_b)
rounded(
    pl.DataFrame(
        {
            "day": [m.index for m in runs_a[0][:6]],
            "action": [m.action for m in runs_a[0][:6]],
            "press was on": [m.before.setup[facts.line] for m in runs_a[0][:6]],
            **{part: [getattr(m.ledger, part) for m in runs_a[0][:6]] for part in ledger_parts[:5]},
            "total": [m.ledger.total for m in runs_a[0][:6]],
        }
    )
)

# %% [markdown]
# **The race.** `race(project, ...)` does all of this for every policy file:
# 1. It tunes each rule on the tuning demand. It tries every value of the rule's `SETTINGS` grid.
# 2. It runs each rule with its best setting on new demand.
# 3. It scores each day, ranks the rules, and keeps the moves of the top rules for the Markov maps.
#
# Every rule runs on the same demand draws.
#
# One detail. The built-in `random` rule keeps its dice between calls, so its numbers depend on what ran before.
# We open the project again, so every rule starts the race fresh.

# %%
project = Project.open(WORK)
settings = RaceSettings(episodes=EPISODES, seed=2024, tune_episodes=TUNE_EPISODES, tune_seed=777)
started = time.time()
result = race(project, settings=settings)
print(f"raced {len(result.ranking())} rules in {time.time() - started:.1f} seconds")
print("rules skipped because of a problem:", len(result.skipped))
tuned = {row.policy: dict(row.settings) for row in result.ranking()}
winner = result.ranking()[0].policy
palette = theme.colors_for([row.policy for row in result.ranking()])
view_model = race_view_model(result, project)  # the race as decision-first data, for the page below
today = view_model["baseline"]["policy"]  # the rule the plant runs today
focus = theme.colors_for(palette, pick=winner, today=today)  # blue is the pick, grey is today, the rest are quiet
print("the winner:", winner, tuned[winner], "| today:", today)

# %% [markdown]
# ## Step 7. Analysis and charts
#
# ### The answer first
# The race result holds every number we need. We start with the decision.
# `decision.show` draws the same view as the report page: what to do, what it is worth against today, and what to watch.
# Every chart below asks one question. A number is a gap from today, the rule the plant runs now.

# %%
decision.show(view_model)

# %% [markdown]
# The functions in `twinflow.extensions.analysis` turn the race result into tables and plotly charts.
#
# ### Final profit table
# Mean and spread of profit per run. **Fill rate** is the share of demand we met.
# **Changeovers** counts the days the press switched product. **Cross-family** counts the costly ones.

# %%
final = pl.DataFrame(
    {
        "policy": [r.policy for r in result.ranking()],
        "mean profit ($)": [r.dollars.mean for r in result.ranking()],
        "std ($)": [r.dollars.std for r in result.ranking()],
        "fill rate (%)": [None if r.fill_rate is None else 100 * r.fill_rate for r in result.ranking()],
        "changeovers / run": [r.changeovers for r in result.ranking()],
        "cross-family / run": [r.cross_family for r in result.ranking()],
        "best setting": [", ".join(f"{k}={v:g}" for k, v in r.settings.items()) for r in result.ranking()],
    }
)
rounded(final)

# %% [markdown]
# ### Reward check: where the money goes
# Up bars are money in. Down bars are money out. The dark dot is net profit.
# The parts must add up to the total. We check that too.

# %%
parts = analysis.parts_per_run(result)
gap = parts.select((pl.col("revenue") - pl.col("production") - pl.col("setup") - pl.col("holding") - pl.col("lost_sales") - pl.col("late") - pl.col("total")).abs().max()).item()
print(f"Largest gap between the parts and the total: {gap:.6f} dollars")

fig = go.Figure()
for part, sign in [("revenue", 1), ("production", -1), ("setup", -1), ("holding", -1), ("lost_sales", -1)]:
    fig.add_bar(name=part, x=parts["policy"], y=sign * parts[part])
fig.add_scatter(x=parts["policy"], y=parts["total"], mode="markers", name="net profit", marker={"color": theme.palette().headline, "size": 10})
fig.update_layout(barmode="relative", yaxis_title="dollars per run", height=500)
theme.question(fig, "Where does the money go for each rule?", "Bars up are money in. Bars down are money out. The dot is what is left.")

# %% [markdown]
# ### Every tuning try, and the winner
# Each dot is one setting of one rule, scored on the tuning demand.
# The diamond is the setting the race kept. A wide spread means the setting matters a lot.

# %%
rounded(analysis.tuning_summary(result).sort("best", descending=True))

# %%
trials = analysis.tuning_trials(result)
fig = go.Figure()
for is_best, symbol, size, color in [(False, "circle", 8, theme.palette().quiet), (True, "diamond", 13, theme.palette().pick)]:
    part = trials.filter(pl.col("best") == is_best)
    fig.add_scatter(
        x=part["policy"], y=part["dollars"], text=part["settings"], mode="markers",
        name="best setting" if is_best else "other tries", marker={"symbol": symbol, "size": size, "color": color},
    )
fig.update_layout(yaxis_title="mean profit on the tuning demand ($)", height=500)
theme.question(fig, "Does the setting matter for each rule?", "A tall column of dots means a bad setting costs real money.")

# %% [markdown]
# ### Profit over time
# The running total of profit, day by day, as a mean over all runs.
# We draw it as the gap from today's rule. Today is the grey zero line. A line above zero is a rule that has earned more so far.
# Then one small chart for each rule, with a band of one standard deviation. The band shows how much runs differ.

# %%
cumulative = analysis.cumulative_profit(result)
running = {name: cumulative.filter(pl.col("policy") == name).sort("day")["mean"].to_list() for name in palette}
gap = theme.vs_today(running, today)
days = cumulative.filter(pl.col("policy") == today).sort("day")["day"].to_list()
fig = go.Figure()
for name in sorted(palette, key=lambda n: n in (today, winner)):  # draw the pick and today last, on top
    fig.add_scatter(x=days, y=gap[name], name=name, mode="lines", line={"color": focus[name], "width": 3 if name in (today, winner) else 1.5})
fig.update_layout(xaxis_title="Day", yaxis_title="dollars ahead of today", height=550)
theme.question(fig, "When does each rule pull ahead of today?", f"{winner} ends the run {theme.delta_text(gap[winner][-1])}.")

# %%
names = list(palette)
columns = 3
fig = make_subplots(rows=-(-len(names) // columns), cols=columns, subplot_titles=names, shared_xaxes=True)
for i, name in enumerate(names):
    part = cumulative.filter(pl.col("policy") == name)
    row, col = i // columns + 1, i % columns + 1
    fig.add_scatter(x=part["day"], y=part["high"], mode="lines", line={"width": 0}, showlegend=False, row=row, col=col)
    fig.add_scatter(
        x=part["day"], y=part["low"], mode="lines", line={"width": 0}, fill="tonexty",
        fillcolor=theme.faded(palette[name]), showlegend=False, row=row, col=col,
    )
    fig.add_scatter(x=part["day"], y=part["mean"], mode="lines", line={"color": palette[name]}, showlegend=False, row=row, col=col)
fig.update_layout(height=900)
theme.question(fig, "How much do runs differ for each rule?", "The line is the mean. The band is one standard deviation.")

# %% [markdown]
# ### Who leads early? Who leads at the end?
# The table ranks the rules (1 is the most profit so far) on several days.
# The chart follows each rule's rank day by day.

# %%
EARLY_DAY = 5
LAST_DAY = model["horizon"]
CHECK_DAYS = [EARLY_DAY, LAST_DAY // 4, LAST_DAY // 2, LAST_DAY]
ranks = result.rank_by_day()
means = result.cumulative("dollars")
table = pl.DataFrame(
    {
        "policy": list(ranks),
        **{f"rank day {d}": [ranks[p][d - 1] for p in ranks] for d in CHECK_DAYS},
        **{f"profit day {d}": [means[p][d - 1] for p in ranks] for d in CHECK_DAYS},
    }
)
rounded(table.sort(f"rank day {LAST_DAY}"))

# %%
early_leaders = [p for p in ranks if ranks[p][EARLY_DAY - 1] == 1]
print(f"Best after {EARLY_DAY} days: {', '.join(early_leaders)}. Best after {LAST_DAY} days: {winner}.")
second = result.ranking()[1].policy
for challenger in (winner, second):
    for leader in early_leaders:
        if leader != challenger:
            print(f"{challenger} passes {leader} on day {analysis.first_day_passed(result, leader, challenger)}")

highlight = list(dict.fromkeys([*early_leaders, winner, second, today, "setup_aware_runout"]))
rank_table = analysis.rank_by_day(result)
fig = go.Figure()
for name in highlight:
    fig.add_scatter(x=rank_table["day"], y=rank_table[name], name=name, mode="lines", line={"color": focus[name]})
fig.update_yaxes(autorange="reversed", title_text="rank (1 is best)")
fig.update_layout(xaxis_title="Day", height=550)
theme.question(fig, "Who leads early, and who leads at the end?", f"{winner} is the pick. Today's rule, {today}, is grey.")

# %% [markdown]
# ### Does the length of the plan change who wins?
# Same rules, same tuned settings. We only change the horizon.

# %%
HORIZONS = [10, 30, 60] if FAST else [10, 30, 60, 120]
horizon_runs = 5 if FAST else 100
# The built-in `random` rule keeps its dice between calls. A fresh project for each plan length gives it fresh dice.
one_column_each = [
    analysis.horizon_table(Project.open(WORK), list(palette), [h], episodes=horizon_runs, seed=7, settings=tuned)
    for h in HORIZONS
]
by_horizon = pl.concat([one_column_each[0], *(t.drop("policy") for t in one_column_each[1:])], how="horizontal")
horizon_columns = [f"{h} days" for h in HORIZONS]
rounded(by_horizon)

# %%
rank_columns = [pl.col(c).rank("min", descending=True).cast(pl.Int64).alias(f"rank, {c}") for c in horizon_columns]
by_horizon.select("policy", *rank_columns)

# %%
fig = go.Figure()
for row in by_horizon.iter_rows(named=True):
    fig.add_scatter(
        x=HORIZONS, y=[row[c] for c in horizon_columns], name=row["policy"], mode="lines+markers",
        line={"color": focus[row["policy"]]},
    )
fig.update_layout(xaxis_title="Days in the plan", yaxis_title="mean profit ($)", height=500)
theme.question(fig, "Does the best rule change with the plan length?", "Lines that cross are a rule that wins short plans and loses long ones.")

# %% [markdown]
# ### The Markov chain map of the winning rule
# Under the winning rule we record where the plant goes each day and count the moves.
# This gives a **transition matrix**: the row is where we are, the column is where we go next.
# With so many products there are far too many exact states to map.
# So the map uses a small **family view**, named `family` here.
# For each color family it asks if the thinnest product is low, medium or high on stock.
# It also notes which family the press is on. We show the most visited states (the code names the number).

# %%
MAP_RUNS = 20 if FAST else 300
map_runs = analysis.run_episodes(project, winner, MAP_RUNS, seed=5, settings=tuned[winner])
map_moves = [m for run in map_runs for m in run]
chain = project.markov(map_moves, "family", top_k=15)
print(f"{len(chain.share)} different family states were visited in {chain.moves:,} days")
print(f"The top 15 cover {chain.coverage:.1%} of all days")
theme.question(analysis.markov_heatmap({"family": chain}), "Where does the plant go next?", "Each row is where we are. Each column is where we go. A bright cell is a likely move.")

# %%
theme.question(analysis.markov_network({"family": chain}), "Which states does the plant visit most?", "Big dots are common states. Thick arrows are likely moves.")

# %% [markdown]
# ### Stock by product by day, one run
# Top: days of cover for every product. Bottom: the same, squashed to low, medium and high.
# Dashed lines mark the days the press changed product.

# %%
theme.question(analysis.inventory_grid(project, map_runs[0]), "Which products run thin, and when?", "Orange is low stock. Dashed lines are the days the press changed product.")

# %% [markdown]
# ### The same race as one web page
# The race result can write one self-contained page. It has the ranking, the charts, the Markov maps
# and captions in plain words. You can send the file to a colleague.

# %%
page = write_race_report(result, WORK / "race-report.html")
print("wrote", page.name, f"({page.stat().st_size / 1024:,.0f} KB)")

# %% [markdown]
# ## Your turn: add your own rule, reward and settings
#
# A company adds its own files to its project folder. Each kind of file has its own folder:
# - `policies/` holds rules, with `decide(state)`.
# - `rewards/` holds scoring rules, with `reward(move)`.
#
# A policy file can also hold a `SETTINGS` grid. The race tunes it for you.
# We write one of each into a new project folder. Then we race them against two built-in rules.
# The new reward file changes how the race scores a day. It adds a pain for every color change.
# The dollars stay the same and are always shown next to the score.

# %%
MY_POLICY = '''\
"""Stay in the color on the press. Switch only when nothing there is low."""

NAME = "stay_in_color"

# Values the race can try, to find the best one for this plant.
SETTINGS = {"floor_days": [2, 4, 6, 8]}


def decide(state, floor_days=4):
    # Products with less than `floor_days` of stock are low.
    low = [p for p in state.products if state.days_of_cover[p] < floor_days]
    if not low:
        return "idle"
    # Prefer a low product in the color the press is already set for.
    color = state.family[state.setup]
    same_color = [p for p in low if state.family[p] == color]
    choices = same_color or low
    return min(choices, key=lambda p: state.days_of_cover[p])
'''

MY_REWARD = '''\
"""Dollars, minus an extra pain for every color change."""

NAME = "calm_press"

# Extra score taken off on a day the press changes color.
SETTINGS = {"color_change_pain": 500.0}


def reward(move, color_change_pain=500.0):
    # A color change costs more than 1,000 dollars here. A size change costs less.
    pain = color_change_pain if move.ledger.setup >= 1000 else 0.0
    return move.ledger.total - pain
'''

MINE = Path(tempfile.mkdtemp(prefix="twinflow-mine-"))
(MINE / "policies").mkdir()
(MINE / "rewards").mkdir()
(MINE / "policies" / "stay_in_color.py").write_text(MY_POLICY)
(MINE / "rewards" / "calm_press.py").write_text(MY_REWARD)

# The capsule picks the score in its experiment section. One new line does it.
mine_capsule = (WORK / "molding-press.twin.yaml").read_text()
mine_capsule = mine_capsule.replace("  policy: lowest_cover", "  score: calm_press\n  policy: lowest_cover")
(MINE / "molding-press.twin.yaml").write_text(mine_capsule)

mine = Project.open(MINE)
print("project is ready:", mine.ok, "| files with problems:", len(mine.extensions.problems))
print("the race scores by:", mine.config.score)

# %%
mine_settings = RaceSettings(episodes=EPISODES, seed=2024, tune_episodes=TUNE_EPISODES, tune_seed=777)
mine_result = race(mine, ["stay_in_color", "lowest_cover", "family_campaign"], settings=mine_settings)
rounded(
    pl.DataFrame(
        {
            "policy": [r.policy for r in mine_result.ranking()],
            "best setting": [", ".join(f"{k}={v:g}" for k, v in r.settings.items()) or "none" for r in mine_result.ranking()],
            "score (calm_press)": [r.score.mean for r in mine_result.ranking()],
            "dollars": [r.dollars.mean for r in mine_result.ranking()],
        }
    )
)

# %% [markdown]
# The new rule raced like every built-in rule. The race tuned its `floor_days` grid.
# The score has the color-change pain in it. The dollars do not.
# A rule that wins on score can lose on dollars. That is why both columns are shown.
#
# A broken file never stops a race. The race skips it and says why, in plain words.

# %%
broken = MINE / "policies" / "broken_rule.py"
broken.write_text('NAME = "broken_rule"\n\n\ndef decide(state):\n    return "make-everything"\n')
with_broken = Project.open(MINE)
skipped = race(with_broken, ["broken_rule", "lowest_cover"], settings=RaceSettings(episodes=2, tune_episodes=1)).skipped
for problem in skipped:
    print(problem.where, "|", problem.message)
    print("  fix:", problem.fix)

# %% [markdown]
# # Conclusion
#
# **Which rule won?** The numbers below come from the race above.
# Nothing here is a learned (RL) policy. All rules are hand-made and tuned in the same fair way.
#
# **What this says about long-term thinking.**
# - Early in the run, a rule that does little can lead. Money spent on changeovers has not paid back yet.
# - Later, the rule that holds a run in one color wins. It pays the cheap changeovers and avoids the costly ones.
# - A rule that looks one day ahead skips the cost today. It slowly runs out of stock and loses sales.
# - The plan length changes the winner. A short plan favors rules that do little.
# - Switching too often hurts, mostly the costly color changes.
#
# **What the Markov map shows.** The plant moves between a small set of family stock situations.
# Under a campaign rule it often stays in one for days.
#
# **What this does not show.** We tuned on one problem, with one set of changeover costs.
# A different cost mix could change the order. And no rule here is the best possible rule.
# They are rules a planner could use today. They are the baseline a learned policy must beat.

# %%
top = result.ranking()
print(f"Winner: {winner}, with ${top[0].dollars.mean:,.1f} per run (std ${top[0].dollars.std:,.1f}). Next: {top[1].policy}, ${top[1].dollars.mean:,.1f}.")
tries = analysis.tuning_summary(result)["settings tried"].max()
print(f"Tuning: each rule had at most {tries} tries, on {TUNE_EPISODES} runs of seed 777. Scored on {EPISODES} runs of seed 2024.")
print()
print(f"After {EARLY_DAY} days the leader was {', '.join(early_leaders)}:")
for name in early_leaders:
    print(f"  {name}: ${means[name][EARLY_DAY - 1]:,.1f} on day {EARLY_DAY}, rank {ranks[name][LAST_DAY - 1]} on day {LAST_DAY} (${means[name][LAST_DAY - 1]:,.1f})")
print(f"{winner} was rank {ranks[winner][EARLY_DAY - 1]} on day {EARLY_DAY}, and passed the early leader(s) on day:")
for name in early_leaders:
    if name != winner:
        print(f"  {name}: day {analysis.first_day_passed(result, name, winner)}")
print()
print("Best rule by plan length:")
for column in horizon_columns:
    leader = by_horizon.sort(column, descending=True).row(0, named=True)
    print(f"  {column}: {leader['policy']} (${leader[column]:,.1f})")
print()
switchers = sorted(top, key=lambda r: -r.changeovers)[:3]
print("Most changeovers per run:", "; ".join(f"{r.policy} {r.changeovers:.1f} ({r.cross_family:.1f} across families)" for r in switchers))
print(f"{winner}: {top[0].changeovers:.1f} changeovers per run, {top[0].cross_family:.1f} across families")
print(f"Markov map: the top 15 family states cover {chain.coverage:.1%} of all days")
