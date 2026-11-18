# twinflow decision views: design

Status: design for TF-006 T-270 and T-271 (REQ-228 to REQ-231). Builders work from this file,
`DATA-CONTRACT.md`, `BUILD-PLAN.md` and the mockups in `mockups/`.

Owner's brief: "The UIs on these reports are atrocious. We should be using modern charting, JS,
UI, UX to make next-level views, analysis, interaction and to really help us understand the data.
The key is to make better decisions in business, not have all the stats."

Readers: plant managers and planners at mid-market manufacturers (smart, not technical),
analysts, and AI agents.

## 0. What is wrong today

The current race report (`twinflow policies race --out`, screenshots in `current/`) is:

- 8.5 MB, because it inlines Plotly. The mockup with the same data is 148 KB.
- 8,800 px of stacked sections. The first screen is one sentence and a 12-row stats table.
- "The winner made $65,410" with no "compared to what". The planner cannot tell if that is good.
- A rank-by-day chart with 12 crossing lines and a Mermaid state chart nobody can read.
- No interaction. No compare, no what-if, no replay.

It reports. It does not help anyone decide.

## 0.1 This is the one standard

Owner decision (2026-11-18): "the new layout is nice, it should be standard for all reports, in
all sections (rl, digital twin, react app, etc)".

So this file is not only the race report design. It defines **the one standard layout, token set
and component set for every twinflow report and view**. Nothing ships with its own look.

**The standard layout** (every report and every workspace page that shows results):

1. **Top bar.** Product mark, view name, project, one line of scope. Theme toggle, Print, Export.
2. **Section bar.** Sticky. Starts with the answer section. Ends with Details.
3. **Answer hero.** Navy band. Eyebrow with the question. A headline that is the answer. A lede
   of two sentences. One to three big figures, each a difference from today where a baseline
   exists. How sure we are, in words. Cautions. On the right, a card with what changes or the
   one thing to do.
4. **Cautions.** Up to three amber-edged cards, right under the hero.
5. **Question sections.** Each section has an eyebrow, a question as its `h2`, and cards. Each
   chart card has a question as its title, a chart, and a one-sentence takeaway.
6. **Details.** Run facts, full tables, method note, problems, downloads. Always last.

**The standard parts:** tokens in section 8, components in section 9, chart choices in 9.4,
words in 8.5, accessibility in 8.4, offline export in section 11.

**Token source of truth** (for `docs/design/VISUALS.md` and every other consumer):

| File | Holds |
|---|---|
| `web/src/index.css` | Brand layer, `--ba-*` (BlackArc palette, type, radius, motion, light and dark). Exists today. Not changed by this design |
| `web/src/decision/tokens.css` | Decision layer, `--tf-*` (pick, today, gain, loss, stock ramp, families, grid, hero). New. The values are the table in 8.1 |
| `web/src/decision/tokens.json` | The same `--ba-*` and `--tf-*` values as JSON, generated from the two CSS files by `web/scripts/export-tokens.mjs`. Python charts, notebooks and docs figures read this file. Never typed by hand |

Until `tokens.css` is built, the table in 8.1 and the `:root` block at the top of
`mockups/race-report.html` are the reference values. Docs visuals must match them. If a doc
figure needs a colour that is not there, add a token here first.

**Where the standard applies, and what replaces what:**

| Surface | Today | Becomes |
|---|---|---|
| RL / policy race report | `src/twinflow/extensions/report.py` (Plotly, Mermaid, 8.5 MB) | Race view (section 3) |
| Digital-twin run report | `src/twinflow/report/html.py` `HtmlReport` (Plotly, Mermaid) | Run viewer (section 4). **Must be migrated** |
| Sweep and optimize output | `summary.json` and console tables | "Best fix" section of the run viewer, and a Sweep view built from the same parts |
| States and Markov | console text, `markov.mmd` | State explorer (section 5) |
| Improve | console text, `improve.json` | Improve view (section 6) |
| Project and extension checks | console text | Project view (section 7) |
| React workspace pages | `web/src/workspace/*` (`ExperimentView`, `ScenarioView`, `DecisionLab`, `DomainResults`, `ActionReview`) and `web/src/LegacyApp.tsx` tabs (`RunTab`, `SweepTab`, `OptimizeTab`, `FloorTab`) | Same `Page`, `Section`, `Card`, hero and chart components from `web/src/decision/`. Result pages embed the views above. Legacy tabs are retired once their view exists |
| Notebooks (`examples/molding-press/notebooks`, RL samples) | Matplotlib and Plotly defaults | A small Python chart theme, `twinflow.report.theme`, that reads `tokens.json` and applies the same colours, fonts and "question as title" rule. A notebook can also show any view inline with `twinflow.report.decision.show(view_model)` |
| Docs figures | hand-made SVG | Follow `docs/design/VISUALS.md`, which takes its tokens from the files above |

Rules for anything new: start from `Page` and `DecisionHero`. If a needed part is missing, add it
to `web/src/decision/` and to section 9 of this file. Do not build a one-off.

## 1. Principles

1. **Decision first.** The first screen says what to do, how much it is worth, how sure we are
   and what changes on the floor. A reader who stops there has what they need.
2. **Today is the unit.** Every headline number is a difference from "what we do today"
   (`experiment.race.baseline`). Raw totals live one click deeper.
3. **One chart, one question.** Each chart title is the question it answers, in the reader's
   words. If a chart has no question, it is cut.
4. **Say the takeaway.** Under each chart sits one sentence that says what it shows. The chart is
   the proof, not the message.
5. **Honest by default.** Estimates carry their label next to the number ("if every 60 days looks
   like these runs"). Quick math is marked as quick math. Cautions sit beside the recommendation,
   not in a footnote.
6. **Plain words.** Sixth-grade reading level. Short sentences. No em dashes. "Rule", not
   "policy". "Changeover", not "setup cost component". "Likely range", not "confidence interval".
7. **Stats one click deeper.** Ranges, seeds, run counts and full tables live in Details and in
   tooltips. Nothing is hidden. Nothing is first.
8. **Same view everywhere.** One set of React components renders in the workspace and in the
   emailed HTML file. Same data contract, same look.
9. **Readable by agents.** The file carries its JSON view model and a plain-text summary, so an
   agent reads facts, not pixels.

### The honesty rule in practice (found in the real data)

The molding-press race gives a clean headline: Family campaign beats today by $22,088 per run and
won 40 of 40 runs. But the day-by-day data shows it is **behind today's rule until day 54 of
60**, and $23,545 behind on day 42. It builds stock first and earns later. A stats table hides
this. A decision view must show it next to the recommendation. This is why the hero has a
"One caution" line and the "When does it pull ahead?" chart. Design rule: **any fact that could
change the decision appears on the first screen.**

## 2. Information architecture

### 2.1 The five views

| View | The question | Source | CLI that writes the offline file |
|---|---|---|---|
| Policy race | Which rule should we run, and what is it worth? | `RaceResult` | `twinflow policies race --out` writes `race.html` |
| Run viewer | Will my orders finish on time, and what would help? | DES run folder | `twinflow run` / `twinflow report <run> --out html` writes `report.html` |
| State explorer | What situations does the plant live in, and which cost money? | any run with captured moves | `twinflow states --html`, `twinflow markov --html` write `states.html` |
| Improve result | Did my new rule win, and is it proven? | `improve.json` | `twinflow improve` writes `improve.html` |
| Project | What is in this twin, and what must I fix? | `Project` + checker | `twinflow extensions check --html` writes `project.html` |

### 2.2 In the workspace (`web/`)

The workspace keeps its current pattern: a left sidebar and `?page=` routing through
`history.replaceState` (see `web/src/workspace/WorkspaceApp.tsx`). No router library is added.

Add one nav group under the current one:

```
WORKSPACE                 DECISIONS
  Scenario library          Project            ?page=project&project=<id>
  Experiments               Policy races       ?page=races            (list)
  Build a twin                                 ?page=race&id=<run>    (one race)
  Operational data          Runs               ?page=runs             (list)
  Scheduling                                   ?page=run&id=<run>     (one run)
  Action review             States             ?page=states&id=<run>
  Agent access              Improve            ?page=improve&id=<run>
```

View state lives in the URL so a link reopens the same picture:

| Param | Meaning | Example |
|---|---|---|
| `tab` | section in view | `tab=replay` |
| `a`, `b` | the two rules in Compare | `a=family_campaign&b=lowest_cover` |
| `day` | replay day | `day=9` |
| `rule` | which rule the state map shows | `rule=lowest_cover` |
| `state` | selected state id | `state=s3` |
| `m` | what-if multipliers | `m=1,2.5,1` |

List pages (`races`, `runs`) are simple tables: name, date, headline sentence, a status pill.
The headline sentence is the view model's `recommendation.sentence`, so the list already answers
the question.

Each view has the same page chrome:

- Title bar: view name, project, one line of scope ("40 runs of 60 days, same customer orders
  for every rule").
- Actions on the right: **Export HTML** (downloads the offline file), theme toggle, Print.
- A sticky section bar (Decision, Why it wins, ...). It highlights the section in view.

### 2.3 As an offline file

The offline file is the same React view with the sidebar removed and a slim top bar (see
mockups). View state goes in the URL hash (`race.html#tab=replay&day=9`) so it survives email
and file shares. Section 9 covers the build.

### 2.4 The reading path of every view

```
Answer (hero)  ->  Why  ->  Explore (compare, what-if, replay, map)  ->  Details
   10 seconds     1 minute            as long as you like              for the analyst
```

## 3. View 1: policy race (the priority)

Mockup: `mockups/race-report.html`. Screenshots: `mockups/race-report-{1280,820}-{light,dark}.png`.
All numbers are from the real race (`twinflow policies race examples/molding-press --episodes 40`).

Sections, top to bottom: Decision, Before you switch, Why it wins, All rules, Compare two,
What if, Floor replay, State map, Money leaks, Tuning, Details.

### 3.1 Decision (hero)

**Purpose.** Tell the reader what to do and what it is worth.
**Decision supported.** Switch rules, or do not.

Layout (1280 px): navy band, two columns.

```
+--------------------------------------------------+--------------------------------+
| RECOMMENDATION . WHAT TO MAKE NEXT               | What changes on the floor      |
| Switch to Family campaign.                       |  Changeovers per run  59 -> 45 |
| It makes $22,088 more in each 60-day run than    |  Orders filled   94.0% -> 96.7%|
| what you do today (Lowest cover). It won 40 of   |  Stock on hand  1,313 -> 3,378 |
| 40 test runs on the same customer orders.        |  Days the press rests  0 -> 12 |
|                                                  |                                |
| +$22,088            +$134,400                    |                                |
| more per 60-day run more per year, if every ...  |                                |
| (o Very likely better) The gain is likely        |                                |
|   between $19,700 and $24,400 per run.           |                                |
|   . . .. .:.:|:. .. . .   <- one dot per run     |                                |
| ! One caution. The gain shows up late. ...       |                                |
| [See why it wins] [Watch the floor] [Test costs] |                                |
+--------------------------------------------------+--------------------------------+
```

Components: `DecisionHero`, `MoneyFigure` (x2), `ConfidenceBadge`, `RunDots`, `CautionLine`,
`FloorChanges`.

Copy rules:

- Headline: `Switch to {label}.` When the recommended rule is the baseline:
  `Keep {label}. Nothing tested beats it.` When confidence is `maybe_better`:
  `{label} may be better. Test more before you switch.`
- Lede: `It makes {delta} more in each {horizon}-day run than what you do today ({baseline}).
  It won {wins} of {runs} test runs on the same customer orders.`
- Per-year figure always carries its condition in the label: "more per year, if every 60 days
  looks like these runs". Round it to the nearest $100.
- Confidence badge shows `confidence.words` from the view model. The line beside it:
  `The gain is likely between {low} and {high} per run.` Round to $100.
- `RunDots`: one dot per test run on a dollar axis that starts at "$0, same as today". A light
  band marks the likely range. A white line marks the average. Caption: "Each dot is one test
  run. All 40 dots are right of zero, so the new rule won every time." When some dots are left
  of zero: "{n} of {runs} dots are left of zero. In those runs today's rule did better."
  This one picture explains "how sure" better than any interval.
- `CautionLine` shows the top entry of `cautions` (see data contract). It is never hidden.
- `FloorChanges`: up to five rows. Each row: plain label, `before -> after`, the change in
  words on the right, one "so what" sentence under it. Changes that need attention (more stock,
  more rest days) are amber. The rest are light blue. Never red or green.

Interactions: buttons jump to sections. Hover a dot to read that run's gain. Nothing else. The
hero is for reading.

States:

- Loading (workspace only): skeleton blocks in the hero shape. The offline file has no loading
  state because data is inline.
- No baseline found: headline `{label} made the most money.` Sub: "We do not know what you do
  today, so we cannot show the gain. Set `experiment.race.baseline` in your project file."
- Score is not dollars (`about.score_is_dollars` false): money figures show dollars anyway and a
  note says "Ranked by {score}. Dollars shown for scale."
- Only one rule raced: "Only one rule ran. Add a second rule to compare."
- Problems (skipped rules): a quiet bar under the hero. "2 rules could not run. See Details."

### 3.2 Before you switch

Three cards with an amber left edge. Each is one caution in plain words: a five-word title and
two short sentences. They come from `cautions` (computed in Python, so the CLI and agents get the
same list). Real examples from the molding-press race:

- **You will hold 2.6 times more stock.** About 3,400 units on the shelf, up from 1,300. Check
  the room and the cash first.
- **Natural products still run short.** Natural is last in the colour cycle. Each natural product
  is out about 1 day in 6.
- **The gain comes late.** The rule builds stock first. It is behind today's rule until day 54
  of 60. Test a longer run before you commit.

If there are no cautions, show one neutral card: "No cautions. The new rule is better on money,
orders filled and stock."

### 3.3 Why it wins

Two cards side by side.

**"What adds up to the gain?"** A horizontal waterfall of the five money parts, as differences
from today, sorted with gains first. Rows are named for what happened, not for the ledger part:
"Fewer changeovers", "More sales", "Fewer lost sales", "Making more product", "Keeping more
stock". A last row shows "Net gain per run". Blue adds profit, orange takes it away. Takeaway:
"Changeovers are the story. Running one colour at a time saves $35,355 per run. That pays for the
extra stock ($13,098) almost three times over."

Why a waterfall: the question is "what adds up to X". A waterfall is the one chart built for
that. A stacked bar per rule (the old report) makes the reader subtract in their head.

**"When does it pull ahead of today?"** A line chart of profit so far minus today's rule, by
day. Today is the flat dashed zero line. The pick is a bold azure line. The next two rules are
thin grey lines for context. Two points are marked: the lowest point ("Day 42: $23,545 behind")
and the crossing ("Passes today on day 54"). Takeaway: "It invests first and earns later. ..."

Why a difference line: the old chart drew 12 cumulative lines that all climb together. The gap
between two of them is the only thing that matters, so we plot the gap.

### 3.4 All rules

**"How does every rule compare to today?"** One row per rule: rank, name, a bar from the zero
line (today), a thin whisker for the likely range, the dollar difference. Today's row is the zero
line itself and shows its own profit ("$43,322 now"). The pick has a `PICK` tag, today has a
`TODAY` tag. Hover a row for a sentence with the range and runs won.

- The axis is cut at plus or minus $26k so the useful rules are readable. Reference floors
  (Random, Do nothing) sit in a closed "Show 4 weaker rules" fold.
- A rule equal to today reads "same as today" in grey, not "$0" in orange.
- Rule names are `label` from the view model. Hover shows `about` (one sentence on how it works).

Why diverging bars: twelve rules against one baseline is a ranking with a sign. Position on a
shared zero line is the most accurate read.

### 3.5 Compare two

**"How do two rules differ, side by side?"** Two pickers (default A = pick, B = today). A
sentence states the result: "Family campaign makes $22,088 more per run than Lowest cover. It won
40 of 40 runs on the same orders." Below it, a table in three groups: Result, On the floor,
Where the money goes. Each row: A's value, a pair of thin bars (A azure, B grey), B's value, and
"A minus B" in a pill. Blue pill: A is better. Orange pill: A is worse. Grey pill: neither is
better by itself (stock on hand, rest days).

The "better" direction per measure is fixed in one place (`MEASURES` table): profit and fill
rate up, costs and changeovers down, stock and rest days neutral.

Empty state: A equals B gives "Pick two different rules."

### 3.6 What if

**"Does the winner change if my costs are different?"** Left card: three sliders, 0 to 3 times
(from `what_if.min/max/step`): "A changeover costs", "Keeping stock costs", "A lost sale costs".
Right card: the live ranking (top 8) with arrows for moves from the real ranking and a takeaway
sentence.

The honest label is a dashed box right under the sliders, always visible:

> **This is a quick estimate.** It re-prices the same 40 runs. The rules act the same as before.
> To let each rule adjust to new costs, change the costs in your project and rerun the race.

Under each slider, a hint says where the winner flips, so the reader does not have to hunt:
"Below 0.45x Setup-aware run-out wins." "Above 2.45x Setup-aware run-out wins." "Family campaign
wins anywhere on this slider." The front end finds these by scanning the slider range.

When the leader changes, the takeaway turns amber: "Setup-aware run-out would win by $1,046 per
run at these costs. Rerun the race with these costs before you act."

Math (from `what_if.formula`): `profit = revenue - production - setup x m1 - holding x m2 -
lost_sales x m3 - late`, on the stored per-rule cost parts. "Reset to my costs" sets all to 1.

### 3.7 Floor replay

**"What does a normal 60 days look like on the floor?"** One typical run (the run whose gain is
closest to the average), the same customer orders for both rules. Two lanes side by side at
1280 px (Today, Pick), stacked at 820 px. Each lane has three strips on one shared day axis:

1. **Press.** One cell per day, coloured by the family being made. A dashed empty cell is a rest
   day. Runs of one colour show as long blocks. Daily colour flips show as confetti. The whole
   story is visible before reading a word.
2. **Setup $.** A small bar per changeover. Tall orange for a colour change ($1,200). Short grey
   for a size change ($200).
3. **Stock.** One row per product, one cell per day. Three blues for days of stock (under 4, 4 to
   10, over 10). Orange for out of stock. Rows are grouped by family.

One scrubber (range input) and a Play button drive both lanes. A frame marks the chosen day on
all strips. Under each lane, four facts for that day (Press makes, Changeovers so far, Products
out today, Profit so far) and one sentence: "Day 9. Colour change to black ($1,200, half the day
lost). Out of stock: BLK-S, BLK-L, ... Profit today: -$226."

Takeaway: "Same orders, two very different floors. In this run, today's rule changes colour 41
times. The pick changes colour 9 times and runs each colour for about a week. Product-days out of
stock: 124 today, 53 with the pick."

Interactions: drag the slider, click any cell to jump to that day, arrow keys step one day,
Play runs at about 3 days a second and stops at day 60. Hover a cell for its sentence.

Why strips: 12 products by 60 days by 2 rules is 1,440 numbers. A heat strip shows the pattern at
once and keeps a row per product, which is how a planner thinks. The old report had no view of
the floor at all.

Scale: above 30 products, the stock strip groups rows by family and expands a family on click.
Above 180 days, cells merge to weeks.

### 3.8 State map

**"What situations does the plant live in, and which ones cost money?"** This replaces the
Mermaid chart.

A state is "how full each family is" plus "what the press is set to". The map uses that
structure instead of a hairball:

- **Lanes.** One column per press setting ("Press on grey", "Press on black", "Press on
  natural"). A move inside a lane keeps the colour. A move across lanes is a colour change. So
  the layout itself shows changeovers.
- **Boxes.** One per state, sorted by time share inside its lane. Each box has a small glyph:
  one bar per family, taller for more stock, orange when low. Then a plain name ("Grey high,
  natural low") and "6.5% . $803 a day".
- **Box edge colour.** Blue: better than a normal day. Grey: worse than a normal day. Orange:
  loses money.
- **Arrows.** Faint by default. Width shows how likely the move is.

Click a box (or focus it and press Enter):

- Arrows out of it turn azure with a percent label at the far end. Arrows into it turn dashed
  grey.
- The side panel shows: the state in words; **Time spent here** ("8.5%", "About 5 days in a
  run"); **Money made per day here** ("$1,383", "A normal day makes $1,090"); **Where the plant
  goes next** (a list with bars: "Stays here 72%", each next state as a link, and "Rare states,
  not on the map 18%"); **Money on a day here** (sales, making product, changeovers, keeping
  stock, lost sales).

A Pick / Today switch redraws the map for the baseline. The contrast is the insight: today's rule
lives in "All stock low" 46% of the time.

A line under the map says what is left out: "The map shows the 15 most common states. They cover
76% of all days."

Layout rule for other views (State explorer, section 5): lanes come from the `setup` part of the
label when there is one. With no setup part, lanes come from the first stock level. With neither,
boxes go in one grid sorted by share. Layout is computed, fixed and repeatable. No force layout.

At 820 px the panel drops under the map. On touch, tap selects.

### 3.9 Money leaks

**"Where does the pick still lose money?"** A ranked list of five states. Each row: the state in
words, share of days, the biggest cost in words, one thing to try, a stacked bar (changeovers,
keeping stock, lost sales), and dollars per run. Example row: "Black and natural low. Press on
black. 5.8% of days. Biggest cost: lost sales. Try starting this colour sooner so it does not run
out. $2,954".

The "try" line is a template keyed on `cause`. Clicking a row selects that state in the map.

### 3.10 Tuning

**"Did each rule get a fair try?"** Left: for the pick, profit for each setting tried, with the
chosen one marked. Takeaway: "16 days is the sweet spot. 8 to 16 days are all close. At 20 days
the shelf fills up and profit drops by $6,500." Right: a table, one row per rule: settings tried,
best setting, the gap from best to worst, and a pill: "steady" or "touchy" (gap above 25% of the
rule's best profit). A touchy rule needs care before a plant relies on it.

A rule with two settings shows as small multiples (one line per value of the second setting).

### 3.11 Details

Run facts (runs, days, seed, baseline), the full table (profit, 95% range, vs today, runs won,
filled, changeovers, colour changes, stock, source), a method note in plain words, the problems
list, and "Download race.json". This is the only place a "95% range" label appears.

### 3.12 Section states (all sections)

| State | What shows |
|---|---|
| Section has no data (for example no moves kept) | A dashed box: one sentence on what is missing and the exact fix. "No day-by-day record was kept for Lowest cover. Rerun the race. Baseline moves are now always kept." |
| A rule broke | It is left out of charts. Details lists it: file, line, message, fix (from `Problem`). |
| Narrow screen | Cards stack. Tables scroll sideways inside their card. Nothing is cut off. |
| Print | Sticky bars become static. Folds open. Play and sliders are hidden. Each section starts on a new page when it fits. |

## 4. View 2: run viewer (job-shop simulation)

Mockup: `mockups/run-viewer.html`. Numbers are from the real cnc-shop run (30 test runs) and a
real `twinflow balance` sweep.

**Decision supported.** Promise these dates or not. Chase which orders. Fix what first.

### 4.1 Answer (hero)

- Eyebrow: "Will my orders finish on time?"
- Headline is the answer, not a title: "Most will. Watch WO-9." Other forms: "Yes. All 10 orders
  have room." / "No. 6 of 10 orders are likely late."
- Lede: "In 30 test runs, about 9 of 10 orders finished on time. WO-9 was late in more than half
  of the runs. Every order at risk is a shaft, and every shaft waits for the shaft mill."
- Three figures: "87% of orders on time. Likely 83% to 91%." / "1.3 late orders in a normal
  run" / "99% how busy the shaft mill is".
- Right card, "The one change that helps most": "Add a second shaft mill. 87% -> 99% on time.
  +11 points." Then "Orders at risk today: 3 of 10, all shafts" and "Everything else has room".
  If no sweep was run, the card says "No fixes tested yet" with the exact command to test one.

### 4.2 Which orders are at risk, and why?

One row per order, sorted by the chance of being late. Each row: order, part and quantity, a
timeline, a status pill, a reason.

- Timeline: a bar from release to the likely finish, a thin whisker for the range of the finish,
  and a dark tick at the due time. Blue when it finishes with room. Orange when the finish may
  pass the due time. All rows share one time axis, so the eye sees which bars reach their ticks.
- Pill: "Late . 58%" (solid orange, chance at least 50%), "At risk . 43%" (orange outline, at
  least 20%), "On time" (blue).
- Reason: one sentence built from where the order waited longest. "Likely 1.1 min late. It waits
  in line at the shaft mill." / "Done 13 min early. The bracket mill has room."

Why this chart: the old report gave on-time % and a lateness table. A planner manages orders, not
percentages. A Gantt of every operation is too much. One bar per order against its due tick is
the least ink that answers "which ones".

Click a row (workspace) to open the order's steps: time at each work centre split into work and
wait.

### 4.3 Where does the line choke?

The routes as a flow (Saw, Mill, Deburr, Inspect), one row per part family. Each step is a card
with "busy %" and a bar. The choke point has an orange frame, a thick orange line into it (work
piles up there) and a "Choke point" pill. A step above 75% gets "Next to choke". Takeaway: "The
shaft mill is the choke point. It is busy 99% of the time. The bracket mill is next at 83% and
will choke if bracket orders grow by a fifth."

For larger shops the flow reuses the workspace `FloorMap` layout (reactflow) with the same node
card. Under 12 steps, plain CSS grid is enough (as in the mockup).

### 4.4 What one change would help most?

A ranked list from the sweep (`twinflow balance` / `optimize`). Each row: the change in plain
words, what it does ("Late orders drop from 1.3 to 0.1 per run"), a bar from today's on-time %
to the new one, and the gain in points. The best has a `BEST` tag. Changes that barely help say
so: "Only 1 point better than two mills. Not worth a machine."

Below: "Not tested yet" ideas with a Test button in the workspace. In the offline file they show
as the command to run.

### 4.5 Details

Per-order table (released, due, likely finish, range, room, late chance), work-centre table
(busy, blocked, starved, queue), labor hours, assumptions, run facts. The existing `report.html`
tables move here unchanged in content.

### 4.6 States

- One replication only: no ranges, no late chance. The hero says "This is one test run. Run 30
  to see how sure this is." with the command.
- Orders that never finish: a red-free but firm banner. "2 orders did not finish. Likely cause:
  the run ended first, or scrap at inspection." (from the existing console warning).
- No due dates in the plan: the risk section is replaced by "No due dates in this plan, so
  nothing can be late. Add `due_date` to see risk."

## 5. View 3: state and Markov explorer

For any run with captured moves, and any view (`full`, `family`, `simple`, or a view from
`model.state_groups`).

**Decision supported.** Find the situations that cost money and decide what to change.

Layout: the State map component (3.8) full width, plus:

- A top bar: **View** picker (family, simple, ...), **Top states** stepper (default 15),
  **Hide rare moves under** stepper (default 5%).
- Hero sentence built from the data: "The plant spends 46% of its days with all stock low. Those
  days make $530. A normal day makes $722."
- Under the map, two cards: **"Where does the money go?"** (the six ledger parts as bars, the
  `twinflow states` summary) and **"Top states"** (table: state, share, money per day, stays).
- With moves from a job shop (no families), the label is the `full` view. The map falls back to
  the grid layout and a note says: "Add `state_groups` to your model to get a clearer map."

States: "No moves were captured for this run. Add `experiment.state_capture: {every_hours: 4}`
and run again." (the text the CLI already prints).

## 6. View 4: improve result

**Decision supported.** Trust the promotion (or the refusal).

```
+--------------------------------------------------------------------------------+
| DID THE NEW RULE WIN?                                                          |
| Yes. my_rule is now your best rule.          [Copied to policies/my_rule.py]   |
| It makes $3,210 more per run than Family campaign, the old best.               |
| (o Very likely better) Likely between $1,400 and $5,000. Won 34 of 40 runs.    |
| . . .: .:|:.: . .   <- RunDots vs the old best                                 |
+--------------------------------------------------------------------------------+
| Proof                                | What changed on the floor               |
|  Rule used: range above zero  PASS   |  FloorChanges vs old best               |
|  Ranked first of 13           PASS   |                                         |
|  Same orders for both         yes    |                                         |
|  Runs, seed, file hash               |                                         |
+--------------------------------------------------------------------------------+
| [Open the full race]                                                           |
```

(Numbers in this sketch are examples. No improve run was made for this design.)

Headlines by outcome:

- Won and copied: "Yes. {candidate} is now your best rule."
- Ranked first but not proven: "Not yet. {candidate} is ahead, but the lead is not proven."
  Sub: "The likely range of the gain goes from -$900 to +$4,100. It must stay above zero. Run
  more test runs to narrow it."
- Lost: "No. {previous} is still your best rule." Sub: "{candidate} made $2,300 less per run."

The proof card is a checklist from `improve.json` (`rule`, `interval`, `versus`, `ranking`,
`episodes`, `seed`, `sha256`). Each line is pass or fail with the number beside it. Reuses
`DecisionHero`, `RunDots`, `ConfidenceBadge`, `FloorChanges`.

## 7. View 5: project

**Decision supported.** Is this twin ready to trust, and what do I fix first?

```
+--------------------------------------------------------------------------------+
| PROJECT . molding-press                                                        |
| Ready. 0 problems to fix.        (or: 2 problems to fix before you can race.)  |
| One molding press, 12 products in 3 families. Each run is 60 days.             |
+----------------------+----------------------+----------------------------------+
| What the twin holds  | Rules                | Problems to fix                  |
|  1 line: press       |  Today: Lowest cover |  1. policies/my_rule.py line 14  |
|  12 products         |  2 of your own       |     decide returned "GRY-X".     |
|  3 families          |  10 built in         |     That is not a product.       |
|  Costs: changeover   |  Score: dollars      |     Fix: return one of GRY-S ... |
|  $200 / $1,200 ...   |  Views: simple,      |  2. ...                          |
|  Assumptions (3)     |  family, full        |                                  |
+----------------------+----------------------+----------------------------------+
| Last results:  Race 17 Nov: Switch to Family campaign (+$22,088 per run)  [Open]|
```

- Problems come first when there are any. Each problem card: where (file and line), what
  happened, the fix (from `Issue.path`, `message`, `suggestion`). Sorted errors first.
- "What the twin holds" reads the capsule: lines, products, families, horizon, costs,
  assumptions, provenance.
- "Rules" lists policy files and built-ins with `label` and `about`, the baseline, the score,
  the observation masks and the state views.
- "Last results" links to the latest race, run and improve views with their headline sentence.

Empty: a new project with no policies says "No rules of your own yet. The 10 built-in rules will
race. Add one with `twinflow new` or copy `policies/lowest_stock_first.py`."

## 8. Design system

### 8.1 Tokens

The workspace token layer is `web/src/index.css` (BlackArc theme, `--ba-*`). The decision views
use it unchanged and add one small layer, `web/src/decision/tokens.css`. Theme follows
`prefers-color-scheme`, and `data-theme="light|dark"` on `<html>` wins both ways, as today.

| Token | Light | Dark | Used for |
|---|---|---|---|
| `--tf-pick` | `#0073fe` | `#5a9cff` | the recommended rule, selected things |
| `--tf-today` | `#7b8794` | `#8493a8` | what we do today, context series |
| `--tf-gain` (text) | `#0055bc` | `#9cc2ff` | money gained, as text |
| `--tf-gain-fill` | `#0073fe` | `#5a9cff` | money gained, as a bar |
| `--tf-loss` (fill) | `#f68d2e` | `#f68d2e` | money lost, out of stock, choke point |
| `--tf-loss-text` | `#8a4c11` | `#f9a85d` | the same, as text (AA on its surface) |
| `--tf-seq-1/2/3` | `#d6e6ff` `#8fbaff` `#2f7de8` | `#1d3a68` `#2f66bd` `#8fbaff` | low, ok, high stock |
| `--tf-out` | `#f68d2e` | `#f68d2e` | out of stock |
| `--tf-fam-1/2/3...` | `#8a94a3` `#0b1220` `#b98a3a` | `#8493a8` `#e6ecf5` `#c99a4a` | product families, in capsule order |
| `--tf-grid` | `#e7ebf0` | `#1c3157` | grid lines |
| `--tf-axis` | `#9aa4b1` | `#6f86a8` | axis lines, faint arrows |
| `--tf-hero-bg` | `#002550` | `#0f1e38` | hero band (always dark ink on navy) |

Colour rules:

- **Two-colour meaning.** Azure is good or chosen. Orange (`--ba-signal`) is bad or needs
  attention. Grey is today or neutral. This matches the theme (`--good`, `--warn`). There is no
  red and no green, so red-green colour blindness is not a problem. The pair was checked with the
  dataviz validator: CVD separation delta E 17.6, normal vision 18.7.
- **Colour is never alone.** Every gain or loss also has a sign (+ or the minus sign), a word
  ("fewer", "more"), or position against a zero line.
- **Text wears text tokens.** Numbers and labels use `--ba-headline`, `--ba-body`, `--ba-meta`.
  Only gain or loss values take `--tf-gain` / `--tf-loss-text`.
- **Family colours** come from the capsule order. The molding-press families are named after
  colours, so the mock maps grey, black, natural to a slate, an ink and a tan. In dark mode
  "black" becomes near-white. Families always carry a text label as well. More than six
  families: use the first five colours and fold the rest into "Other".
- **Sequential stock ramp** is one hue, light to dark in light mode and dark to light in dark
  mode, so "more stock" is always the stronger step.

Type (existing): display `Space Grotesk` 500 for headings, body `Inter` 400/500, numbers
`JetBrains Mono` with `tabular-nums`. Sizes: hero h1 `clamp(32px, 4vw, 48px)`, section h2 26,
card h3 17, body 15, small 13.5, meta 12.5, chart text 11.5, eyebrow 11 mono caps with 0.16em
tracking. Hero money 44 mono.

Shape (existing): 2 px controls, 4 px surfaces, 1 px borders, no shadows. Depth comes from
borders and ground colour.

Space: 4 px base. Card padding 20. Card gap 16. Section top padding 44. Content max width 1200
with 32 px gutters (20 px under 900 px).

### 8.2 Breakpoints

| Width | Layout |
|---|---|
| 1280 and up | Two-column hero and card grids. State map with a 330 px side panel. Replay lanes side by side. |
| 820 to 900 | One column. Hero stacks (decision, then floor changes). Panel under the map. Replay lanes stacked. Leaderboard keeps bars, with narrower name and value columns. |
| under 600 | Supported, not tuned. Tables scroll inside their card. |

Charts measure their container and redraw on resize. They do not scale a fixed viewBox, so text
stays the same size at every width.

### 8.3 Motion

- Micro changes (hover, select, pill colour): `--ba-motion-micro` (170 ms ease).
- What-if rows reorder with a 170 ms FLIP move so the eye can follow a rank change.
- State map: arrows fade in 170 ms on select. Boxes never move.
- Replay Play: about 3 days a second, no easing. It stops at the last day.
- No entrance animations and no count-up numbers. A report must be readable the instant it opens
  and must print right.
- `prefers-reduced-motion: reduce`: all transitions off. Play steps without fades. Smooth scroll
  off.

### 8.4 Accessibility

Target: WCAG 2.2 AA.

- **Contrast.** Body text at least 4.5:1, large text and chart marks at least 3:1, in both
  themes. Orange is never used for body text on a light surface (`--tf-loss-text` is). CI checks
  this with axe in Playwright.
- **Keyboard.** Everything works without a mouse. Tab order follows the page. Section bar: links.
  State boxes: `role="button"`, `tabindex="0"`, Enter or Space selects, arrow keys move to the
  next box in the lane or the next lane. Replay: the slider takes arrow keys (1 day), Page Up or
  Down (10 days), Home, End. Sliders in What if are native range inputs. Focus ring: 2 px
  `--ba-focus`, 2 px offset (existing rule).
- **Screen readers.** Every chart has `role="img"` and an `aria-label` that states the takeaway,
  not the chart type. Every chart has a "Show as table" toggle that swaps in a real table with
  the same data. The state panel and the what-if takeaway are `aria-live="polite"`. The hero is
  plain text and needs nothing.
- **Not colour alone.** See 8.1. Status pills carry words ("Late . 58%"). Out of stock is orange
  and also named in the day sentence.
- **Touch.** Hit targets at least 40 px. Tooltips also open on tap and on focus.
- **Language.** `lang="en"`. Plain words. Numbers use a real minus sign and thousands separators.
- **Print.** Covered in 3.12.

### 8.5 Words (the glossary the UI uses)

| Say | Not |
|---|---|
| rule | policy |
| test run / run | episode, replication |
| what you do today | baseline |
| pick | winner, recommended policy |
| likely range | confidence interval |
| changeover / colour change / size change | setup, cross-family setup |
| keeping stock | holding cost |
| orders filled from stock | fill rate |
| situation / state | Markov state |
| where the plant goes next | transition |
| choke point | bottleneck |
| busy | utilization |

The CLI keeps its own words. Tooltips may add the technical word in brackets for analysts:
"likely range (95% confidence interval)".

## 9. Component inventory

All in `web/src/decision/`. Props are TypeScript shapes. "Data" names the view-model fields
(see `DATA-CONTRACT.md`).

### 9.1 Primitives

| Component | Props | Notes |
|---|---|---|
| `Page` | `title, scope, sections[], actions?, shell: "workspace" \| "offline"` | Top bar, sticky section bar with scroll spy, footer |
| `Section` | `id, eyebrow, question, hint?, actions?` | `h2` is the question |
| `Card` | `question?, sub?, children, takeaway?, tone?: "info" \| "warn"` | Chart card with the takeaway slot |
| `Takeaway` | `tone, children` | The one-sentence box |
| `Money` | `value, signed?, round?, compact?` | Formats `$22,088`, `+$22,088`, real minus sign |
| `Delta` | `value, unit, better: "up" \| "down" \| "none"` | Pill, blue or orange or grey |
| `Tag` | `kind: "pick" \| "today" \| "best" \| "plain"` | |
| `StatusPill` | `kind: "ok" \| "risk" \| "late", label` | |
| `Segmented` | `options[], value, onChange` | Pick / Today switch |
| `EmptyState` | `title, body, fix?` (a command or setting) | Dashed box |
| `ChartFrame` | `label, table: () => ReactNode, children(width)` | Measures width, `role="img"`, "Show as table" toggle, tooltip host |
| `Tooltip` | internal | Hover, focus and tap |
| `useViewState` | `(key, default)` | Reads and writes query (workspace) or hash (offline) |

### 9.2 Decision components

| Component | Props | Data it needs |
|---|---|---|
| `DecisionHero` | `eyebrow, headline, lede, figures[], confidence, dots?, caution?, actions[], side` | `recommendation`, `baseline`, `delta`, `confidence`, `cautions[0]` |
| `MoneyFigure` | `value, label, size` | `delta.per_run`, `delta.per_year`, `delta.assumption` |
| `ConfidenceBadge` | `level, words, detail` | `confidence.level/words/detail` |
| `RunDots` | `gaps: number[], mean, low, high, zeroLabel` | `confidence.gaps` (new) |
| `CautionLine`, `CautionCards` | `cautions[]` | `cautions` (new) |
| `FloorChanges` | `rows[]` | `floor[]` |
| `Waterfall` | `items: {label, value}[], totalLabel` | `drivers[]` |
| `GapLine` | `series: {id, label, values[], emphasis}[], marks[]` | `policies[].profit_by_day`, `timing` (new) |
| `Leaderboard` | `rows[], limit, foldAfter` | `policies[].vs_baseline`, `rank`, `label`, `about` |
| `CompareTable` | `a, b, measures[]` | `policies[]` (all fields), `policies[].dollars_by_run` (new) |
| `WhatIf` | `policies[], config, pick` | `what_if`, `policies[].parts` |
| `ReplayLanes` | `runs: {label, tag, days[]}[], products[], families[], day, onDay` | `example_runs`, `products`, `families` |
| `PressStrip`, `SetupStrip`, `StockStrip` | parts of a lane, share an x scale | `example_runs.*.days[]` |
| `DayFacts` | `day, run` | same |
| `StateMap` | `nodes[], edges[], lanesBy, selected, onSelect, avgPerDay` | `markov[rule].nodes/edges` |
| `StateGlyph` | `levels, order, families` | `nodes[].parts_of_label`, `views.levels` (new) |
| `StatePanel` | `node, edges, avgPerDay, horizon` | same |
| `LeakList` | `leaks[], onSelectState` | `leaks[rule]` |
| `TuningCurve` | `trials[], chosen` | `tuning[]` |
| `TuningTable` | `rows[]` | `tuning[]` |
| `FullTable` | `policies[]` | `policies[]` |
| `RunFacts`, `MethodNote`, `ProblemList` | | `about`, `warnings`, `problems` |

### 9.3 Run viewer components

| Component | Props | Data |
|---|---|---|
| `OrderRiskList` | `orders[], horizon` | `orders[]` |
| `OrderTimeline` | `release, finish, low, high, due, tone` | same |
| `ChokeFlow` | `routes[], steps[]` | `routes[]`, `centres[]` |
| `StepCard` | `name, busy, status` | `centres[]` |
| `LeverList` | `levers[], base` | `levers[]` |
| `OrderSteps` (drill-in) | `order` | `orders[].steps[]` |

### 9.4 Chart choices in one table

| Question | Chart | Why this one |
|---|---|---|
| How sure are we? | Dot strip, one dot per run | Shows every run and the zero line. No statistics words needed |
| What adds up to the gain? | Horizontal waterfall | Built for "parts of a difference" |
| When does it pull ahead? | Difference line against a zero line | Plots the gap itself, not two totals |
| How does every rule compare to today? | Diverging bars with whiskers | Ranking with a sign on a shared zero |
| How do two rules differ? | Table with paired bars and delta pills | Mixed units cannot share one axis |
| Does the winner change? | Live ranked bars | The answer is an order, so show the order |
| What does the floor look like? | Day strips (press, setup, stock) | 1,440 values as a pattern, one row per product |
| What situations cost money? | Lane map with glyph boxes | Uses the state's real structure. No hairball |
| Where does it leak? | Ranked list with stacked bars | Five items. A list beats a chart |
| Did tuning work? | Line with a marked point | One setting, one measure |
| Which orders are at risk? | One bar per order to a due tick | Least ink that shows "which ones" |
| Where does it choke? | Flow of step cards | The route is a flow. Keep it a flow |

Not used, on purpose: pie charts, dual axes, 3D, rainbow scales, rank-by-day spaghetti,
force-directed graphs, gauges.

## 10. Libraries

Prefer what `web/` already has. The decision views add no runtime library.

| Library | Version to pin | Role | Change |
|---|---|---|---|
| `react`, `react-dom` | 18.3.1 | UI | Already in `web/`. Pin exact (drop the caret) |
| `recharts` | 2.15.4 | `GapLine`, `TuningCurve`, WIP line in the run viewer | Already in `web/` (lockfile resolves 2.15.4). Pin exact |
| `reactflow` | 11.11.4 | Existing `FloorMap` only. Run viewer flow for shops above 12 steps | Already in `web/`. Pin exact. **Not** in the offline race bundle |
| `@tanstack/react-query` | 5.102.8 | Workspace data loading only | Already in `web/`. Not in the offline bundle |
| `vite` | 8.3.0 | Build | Already in `web/` |
| `@vitejs/plugin-react` | 6.1.1 | Build | Already in `web/` |
| `typescript` | 5.9.3 | Types | Already in `web/` |
| `@playwright/test` | 1.63.0 | Browser tests | Already in `web/` |
| `@axe-core/playwright` | pin exact at install | Accessibility checks in CI | **New, dev only.** Needed to hold the AA line automatically |
| `vitest` | pin exact at install | Unit tests for pure functions (what-if math, layout, formatters) | **New, dev only.** `web/` has no unit test runner today |
| Fonts: Inter, Space Grotesk, JetBrains Mono (woff2, latin subset) | pin files by SHA-256 | Offline type | **New files**, vendored under `web/src/decision/fonts/` with their OFL licence texts. The workspace loads these from Google Fonts today. The offline file must not use the network |

Why most charts are hand-built and not Recharts or a new chart library:

- The waterfall, diverging bars, compare bars, leak bars and order timelines are CSS grid rows
  with positioned `div`s. They are real text, they wrap and scale with the layout, they are
  readable by screen readers, and they cost almost no bytes.
- `RunDots`, the replay strips and the state map are small custom SVG components. No chart
  library has the day-strip or the lane map. The state map is about 250 lines with a fixed
  layout. `reactflow` would add about 150 KB to every emailed file for pan and zoom we do not
  want.
- Recharts stays for the two true line charts, so we keep one charting dependency.

Size budget for an offline file: under 600 KB without fonts, under 900 KB with fonts, plus data.
(The current race report is 8.5 MB.) CI fails the build above the budget.

Licences: React, Recharts (and its d3 parts), reactflow are MIT or ISC. Fonts are OFL 1.1. The
build writes `THIRD-PARTY-LICENSES.txt` and the report links to it from Details (REQ-231:
"license files kept").

## 11. Offline export

Goal: every view exports as one self-contained HTML file. Same components. Data inlined. No
network. Opens from email, a file share or a USB stick.

### 11.1 Build

A second Vite entry builds a single template:

```
web/
  report.html                  # entry for the offline bundle
  src/report/main.tsx          # reads the inlined JSON, picks the view, renders it
  src/decision/*               # the shared components (also used by the workspace)
  vite.report.config.ts        # build config for the single file
  scripts/inline-report.mjs    # folds the one JS and one CSS file into the HTML
```

`vite.report.config.ts`:

- `build.rollupOptions.input = "report.html"`, `output.inlineDynamicImports = true` (one JS
  chunk), `build.cssCodeSplit = false` (one CSS file), `build.assetsInlineLimit` set high so
  fonts become `data:` URIs, `build.modulePreload = false`, `base = "./"`.
- `scripts/inline-report.mjs` (about 30 lines, no dependency) replaces the `<script src>` and
  `<link rel="stylesheet">` tags with inline `<script type="module">` and `<style>`, checks that
  no `http://`, `https://` or `//` URL is left in `src`/`href`/`url()`, checks the size budget,
  and writes `src/twinflow/report/assets/report.template.html`.

A plugin such as `vite-plugin-singlefile` does the same job. We prefer the small script: no new
dependency, no risk of a plugin lagging behind Vite 8, and the no-network check lives in the
same place. (Open decision 5.)

### 11.2 Data injection (Python)

The template holds one placeholder:

```html
<script type="application/json" id="twinflow-data">/*__TWINFLOW_DATA__*/</script>
```

Python (`twinflow.report.decision.render(view_model) -> str`) replaces the placeholder with
`json.dumps(view_model)`, with `</` escaped as `<\/` and U+2028/2029 escaped. It also fills
`<title>` and a `<noscript>` block with the headline sentence, the cautions and the top table as
plain HTML, so the file still says something useful in a mail preview or with scripts blocked.

One template serves all five views. The view model's top-level `view` field (`"race"`, `"run"`,
`"states"`, `"improve"`, `"project"`) picks the page.

The built template is committed to the Python package, so `pip install twinflow` users do not
need Node. CI rebuilds it and fails if the committed file is stale.

### 11.3 Workspace export

The Export HTML button calls `GET /api/v1/decision/{view}/{id}/export`. The API runs the same
Python render and returns the file. The workspace views themselves call
`GET /api/v1/decision/{view}/{id}` and get the same JSON the file inlines. One contract, two
shells.

### 11.4 For AI agents

- The JSON in `#twinflow-data` is the full view model. An agent parses that tag and never reads
  the DOM.
- The view model has `agent_summary`: 5 to 10 plain sentences (decision, gain, confidence,
  cautions, floor changes). It is also written into the `<noscript>` block and into
  `<meta name="description">`.
- Every section root has `data-section="<id>"` and every chart has an `aria-label` that states
  its takeaway.
- The CLI keeps writing `race.json` beside `race.html`.

### 11.5 Safety

All strings from the model (rule names, product ids, file paths, messages) render through React
text nodes. No `dangerouslySetInnerHTML` with model data. The template carries a strict CSP meta
tag: `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:;
font-src data:`. That also proves the no-network rule in the browser.

## 12. Mockups

| File | What it shows |
|---|---|
| `mockups/race-report.html` | The full race view with live compare, what-if, replay and state map. Real numbers |
| `mockups/run-viewer.html` | The run viewer. Real numbers from cnc-shop |
| `mockups/*-1280-light.png`, `*-1280-dark.png`, `*-820-light.png`, `*-820-dark.png` | Full-page screenshots of both |

Open a mockup with `?theme=dark` or `?theme=light` to force a theme. The mockups are plain HTML,
CSS and a little inline script. They are a picture of the target, not code to copy. Known
shortcuts in the mockups: system fallback fonts if the brand fonts are not installed, no "Show as
table" toggles, no arrow-key moves between state boxes, the three caution cards are typed by hand
(they come from `cautions` in the real build), late chance in the run viewer is estimated from
the range (the real build counts runs).
