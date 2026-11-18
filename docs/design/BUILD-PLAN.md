# twinflow decision views: build plan

For builders. Read `DESIGN.md` (what to build) and `DATA-CONTRACT.md` (what data it reads)
first. Open `mockups/race-report.html` and `mockups/run-viewer.html` in a browser. They are the
target picture, not code to copy.

Ground rules:

- TDD. Write the failing test first, then the code (project rule).
- Each task is one small commit on a `feat/` branch. Tasks marked **[P]** can run in parallel
  with their neighbours (different files).
- Model per task: **Opus** for tricky interactive or stateful pieces, **Sonnet** for
  straightforward components and wiring, **Haiku** only for copy and docs.
- All new front-end code lives in `web/src/decision/`. All user-facing text follows the word
  list in `DESIGN.md` 8.5: sixth-grade level, short sentences, no em dashes.
- This design is the standard for every report and view (`DESIGN.md` 0.1). Phase F migrates each
  existing report onto it.

Task ids are `B-01` and up. They are local to this plan. They cover PLAN.md T-270 and T-271.

## Test approach

| Layer | Tool | What it proves |
|---|---|---|
| Unit (Python) | pytest | View-model builders: fields, rounding, sentences, cautions, schema. No em dashes in any string |
| Unit (TS) | vitest (new dev dependency) | Pure functions: what-if re-rank and flip points, state-map layout, state words, formatters, replay day facts, typical-run pick |
| Component | Playwright against a fixtures page (`web/fixtures.html`) that mounts one component with the golden JSON | Each component renders, has its `aria-label`, and its table view matches its data |
| Interaction | Playwright on the built offline file | Compare, what-if, replay scrub and keys, state select and keys, theme toggle, URL hash restore |
| Accessibility | `@axe-core/playwright` on each view, light and dark | No AA failures. Keyboard-only walk-through passes |
| Visual | Playwright screenshots at 1280 and 820, light and dark, saved under `web/tests/browser/__screens__/` | A reviewer (a model with vision, then the owner) compares them with `mockups/*.png` and writes pass or the gaps. Pixel diffs guard later changes |
| Offline | A Playwright test with the network blocked (`context.route("**", abort)` for non-file URLs) plus a grep for `http` in the built file | The file needs no network |
| Size | Script in CI | Offline template under 900 KB |
| Regression | Existing `tests/extensions/test_ext_race_report.py`, `tests/report/*` | Old report tests still pass or are replaced one for one (listed in the migration task) |

A task is done when: its tests pass, `npm run build` and `ruff`/`mypy` pass, and its screenshots
were looked at, not only generated.

---

## Phase A. Foundations

[x] **B-01 Tokens file.** Sonnet.
Create `web/src/decision/tokens.css` with the `--tf-*` tokens from `DESIGN.md` 8.1 (light, dark
by media query, dark and light by `data-theme`). Import it from `web/src/main.tsx`. Add
`web/scripts/export-tokens.mjs` that writes `web/src/decision/tokens.json` from `index.css` and
`tokens.css`.
Accept: every token in the 8.1 table exists in both themes; `tokens.json` is generated and
committed; a vitest test fails if the JSON is stale; text tokens meet 4.5:1 and mark tokens 3:1
on their surface (contrast test).

[x] **B-02 Offline build pipeline.** Opus.
`web/report.html`, `web/src/report/main.tsx` (reads `#twinflow-data`, switches on `view`),
`web/vite.report.config.ts`, `web/scripts/inline-report.mjs`, npm script `build:report`. Output:
`src/twinflow/report/assets/report.template.html` with the data placeholder, CSP meta tag and
`THIRD-PARTY-LICENSES.txt`. Vendor the three fonts (woff2, latin) with OFL texts.
Accept: one HTML file, no external URL, under 900 KB; opens from `file://` with the network
blocked and renders a stub "race" page from the golden JSON; CI job fails when the committed
template differs from a fresh build.

[x] **B-03 Python render.** Sonnet. [P with B-02 once the placeholder name is fixed]
`twinflow.report.decision.render(view_model) -> str` and `write(path, view_model)`. Escapes
`</`, U+2028, U+2029. Fills `<title>`, `<meta name="description">` and `<noscript>` from
`agent_summary`.
Accept: pytest: output parses as HTML; the JSON round-trips; a rule name with `</script>` and
quotes does not break out; no network URL in the output.

[x] **B-04 Schemas and types.** Sonnet. [P]
JSON Schema per view model in `src/twinflow/report/schemas/`. Generate
`web/src/decision/types.ts` from them (`npm run gen:types`).
Accept: `race-view-model.example.json` validates; a missing required field fails; generated
types compile; CI fails when types are stale.

**B-05 Race view-model gaps R1 to R20.** Opus. (Python, `extensions/report_model.py`,
`extensions/race.py`)
Add the fields in `DATA-CONTRACT.md` 1.3. The ones that change engine behaviour: R12 (always
keep the baseline's moves), R7 and R8 (tally stock by day and out-of-stock share for every
policy while streaming), R9 (typical run, same run for both rules), R5 (paired gap by day with
band, pass day, lowest point), R6 (cautions).
Accept: unit tests on the golden molding-press race (40 runs, seed 0): `confidence.gaps` has 40
values, all above zero; `timing.pass_day == 54`; `timing.lowest == {day: 42, dollars: -23545}`
(within rounding); cautions are `late_gain`, `more_stock`, `runs_short` in that order;
`example_runs` use one episode for both rules; `markov` has an entry for the baseline; memory
stays flat when policies grow (the T-239 streaming rule still holds).

[x] **B-06 Shell primitives.** Sonnet.
`Page`, `Section`, `Card`, `Takeaway`, `Money`, `Delta`, `Tag`, `StatusPill`, `Segmented`,
`EmptyState`, `useViewState` (query in the workspace, hash in the offline file), scroll spy.
Accept: fixtures page shows each; `Money` unit tests (`+$22,088`, real minus sign, rounding);
section bar highlights on scroll and works by keyboard; print style makes bars static.

[x] **B-07 ChartFrame and Tooltip.** Opus.
Measures width with `ResizeObserver` and passes it down; `role="img"` and `aria-label`; "Show as
table" toggle; tooltip on hover, focus and tap; honours reduced motion.
Accept: Playwright: resize from 1280 to 820 redraws without clipped text; the table toggle shows
the same numbers; tooltip opens on keyboard focus.

## Phase B. Race view, the decision (first screen)

[x] **B-08 DecisionHero, MoneyFigure, ConfidenceBadge, CautionLine, FloorChanges.** Sonnet.
Per `DESIGN.md` 3.1 with all headline variants and empty states.
Accept: with the golden JSON the hero reads "Switch to Family campaign.", "+$22,088",
"+$134,400", "Very likely better", the caution line, four or five floor rows; variants for
`same_policy`, `maybe_better`, no baseline and one rule are covered by fixture tests; screenshot
matches the mockup hero at 1280 and 820, light and dark.

[x] **B-09 RunDots.** Opus.
Dot strip with stacking, likely-range band, mean line, zero label, caption that changes when
some dots are left of zero.
Accept: 40 dots drawn, none overlap, hover and focus read "One run: +$22,133 vs today"; with a
fixture where 6 gaps are negative the caption says so; table view lists the gaps.

[x] **B-10 CautionCards.** Sonnet. [P]
Accept: up to three cards from `cautions`; the neutral card when the list is empty.

[x] **B-11 Waterfall.** Sonnet. [P]
Accept: rows sorted gains first; bars start where the last one ended; total row equals
`delta.per_run`; takeaway sentence from `recommendation.why`.

[x] **B-12 GapLine.** Sonnet. [P] (Recharts)
Accept: zero line labelled "Today"; pick line bold; the two marks ("Day 42: $23,545 behind",
"Passes today on day 54") come from `timing`; direct labels at line ends do not overlap at
820 px.

[x] **B-13 Leaderboard.** Sonnet. [P]
Accept: today is the zero row; whiskers from `vs_baseline.low/high`; axis cut and fold for weak
rules; "same as today" for a zero gap; hover text has range and runs won.

**B-14 Race page assembly and offline wiring for sections so far.** Sonnet.
`web/src/decision/views/RaceView.tsx`; `twinflow policies race --out` writes the new `race.html`
through B-03 (old writer kept behind `--legacy-report` until B-33).
Accept: running the CLI on `examples/molding-press` gives a file that opens offline and shows
the decision, cautions, why and all rules; Playwright smoke test passes; file under 1.2 MB.

## Phase C. Race view, explore

**B-15 CompareTable.** Sonnet.
Accept: default A is the pick and B is today; picking Reorder point vs Lowest cover reads "makes
$13,691 more per run ... won 36 of 40 runs"; pill colours follow the `MEASURES` direction table;
`a` and `b` persist in the URL.

**B-16 What-if engine (pure).** Opus.
`rerank(policies, m)` and `flipPoints(policies, pick, config)` in `web/src/decision/whatif.ts`.
Accept (vitest): at 1, 1, 1 the order equals the real ranking; holding at 3 times puts
Setup-aware run-out first by $1,046; flip points for the golden data are setup 0.45, holding
2.45, none for lost sales (within one slider step).

**B-17 WhatIf UI.** Sonnet.
Sliders, flip hints, live ranking with move arrows and FLIP motion, amber takeaway on a new
leader, the honest label always visible, Reset.
Accept: Playwright: set "Keeping stock costs" to 3, the takeaway names Setup-aware run-out and
turns amber; Reset restores; the honest label text is present and not hidden at any width;
sliders work by keyboard.

**B-18 Replay strips.** Opus.
`PressStrip`, `SetupStrip`, `StockStrip` on one shared scale; day frame; cell tooltips; family
grouping; merge rules above 30 products or 180 days.
Accept: for the golden run the Today press strip has 41 colour changes and the Pick strip 9;
out-of-stock cells are orange and total 124 and 53 product-days; a 200-day fixture draws week
cells; table view gives day rows.

**B-19 ReplayLanes, scrubber, DayFacts.** Opus.
One slider and Play for both lanes; click a cell to jump; keys (arrows, Page Up/Down, Home,
End); day sentence; `day` in the URL; reduced-motion behaviour.
Accept: Playwright: set day 9, the Pick lane says "Colour change to black ($1,200, half the day
lost)" and "7 of 12" products out; Play advances and stops at 60; side by side at 1280, stacked
at 820.

**B-20 State map layout and words (pure).** Opus.
`layoutStates(nodes, edges, lanesBy)` (lanes by setup, fallback by first level, fallback grid)
and `stateWords(parts_of_label, levels)`.
Accept (vitest): golden pick chain gives three lanes with 6, 6 and 3 boxes sorted by share; no
two boxes overlap; a chain with no `parts_of_label` gives the grid; words for
`grey:high black:ok natural:low | black` are "Grey high, natural low" and "Press on black".

**B-21 StateMap, StateGlyph, StatePanel.** Opus.
SVG map, select by click and keyboard (arrows move within and across lanes), arrows out and in,
percent labels, panel with time, money, next states and money parts, Pick / Today switch,
coverage line.
Accept: Playwright: select "All stock OK. Press on grey." and the panel shows 8.5%, $1,383,
"Stays here 72%"; switching to Today selects "All stock low. Press on grey."; a next-state link
moves the selection; axe passes; panel sits under the map at 820.

**B-22 LeakList.** Sonnet. [P]
Accept: five rows with words, cause, "try" line and stacked bar; clicking a row selects the state
in the map and scrolls to it.

**B-23 TuningCurve and TuningTable.** Sonnet. [P]
Accept: chosen setting marked; "touchy" pill from `tuning[].touchy`; two-setting rules draw
small multiples.

**B-24 Details.** Sonnet. [P]
`FullTable`, `RunFacts`, `MethodNote`, `ProblemList`, Download JSON (a `data:` link built from
the inlined model).
Accept: 12 rows; a fixture with a broken rule lists file, line, message and fix.

**B-25 Race view complete: review gate.** Opus (review), then owner.
Full Playwright interaction suite, axe in both themes, screenshots at 1280 and 820 in light and
dark compared with `mockups/race-report-*.png`, size check, offline check.
Accept: a written review lists every difference from the mockup and each is fixed or agreed.
This closes T-270.

## Phase D. Run viewer and the other views (T-271)

**B-26 Run view-model builder and gaps G1 to G9.** Opus. (Python)
`run_view_model(run_dir)` per `DATA-CONTRACT.md` 2. Includes per-rep lateness, per-order wait by
centre, plan copy in the run folder, sweep link and lever labels, display time unit.
Accept: on the cnc-shop run (30 reps, seed 0): `answer.on_time.mean` is 0.873; `watch` starts
with `wo-9`; `choke.centre` is `mill_shaft`; `late_chance` is a counted share; with a linked
sweep `levers[0].on_time` is 0.987; a one-rep run gives `late_chance: null` and the "one test
run" answer.

**B-27 Run viewer components.** Sonnet.
`OrderRiskList`, `OrderTimeline`, `ChokeFlow`, `StepCard`, `LeverList`, `OrderSteps`; hero
variants from `answer.level`.
Accept: matches `mockups/run-viewer-*.png`; rows sorted by late chance; pills carry words;
states for one rep, no due dates and unfinished orders are covered.

**B-28 States view-model and view.** Sonnet. (Python + TS)
Shared `markov_view()` used by race and states (S1); `states_view_model`; `StatesView` with
view, top and min-prob controls.
Accept: `twinflow states out --html` and `twinflow markov out --html` write `states.html`; the
family view draws lanes; a job-shop run with no families draws the grid with the note; the
no-moves case shows the fix text.

**B-29 Improve view.** Sonnet.
`improve_view_model` (I1 to I5) and `ImproveView` (hero, dots, proof checklist, floor changes,
link to the race).
Accept: three fixture outcomes (won, ahead but not proven, lost) show the right headline and
checks; `twinflow improve` writes `improve.html` next to `improve.json`.

**B-30 Project view.** Sonnet.
`project_view_model` (P1 to P3) and `ProjectView`.
Accept: molding-press shows "Ready. 0 problems to fix.", 12 products in 3 families, 2 own rules
and 10 built in; a fixture with a bad policy file shows the problem with file, line and fix
first; `twinflow extensions check --html` writes `project.html`.

## Phase E. Workspace

**B-31 API endpoints.** Sonnet.
The calls in `DATA-CONTRACT.md` 7, including `/export`.
Accept: API tests return schema-valid models; export returns the same bytes the CLI writes.

**B-32 Workspace navigation and pages.** Sonnet.
"DECISIONS" nav group, list pages (races, runs) with the headline sentence per row, view pages
that mount the same view components with `shell="workspace"`, Export HTML button, URL params.
Accept: Playwright: open a race from the list, change compare and day, reload, state is
restored; Export downloads a file that opens offline to the same view.

## Phase F. Migration: every existing report onto the standard

One task per existing surface. Each task lists what it replaces and how the old tests move.

**B-33 Migrate the race report (`src/twinflow/extensions/report.py`).** Sonnet.
Replace the Plotly and Mermaid writer with `race_view_model` + `render`. Remove
`--legacy-report`. Drop Plotly and Mermaid from this path.
Feature inventory (every old section maps to a new place): The winner -> hero; Ranking ->
All rules + Details table; Profit over time -> "When does it pull ahead"; Where the money goes
-> waterfall + Compare; Rank by day -> dropped as a chart, kept as `timing` facts and the
early-leader sentence in Details; Tuning trials -> Tuning; Profit by view -> Details ("What is
information worth", a small table from `views.dollars`); Markov maps -> State map; Money leaks
-> Money leaks; Problems -> Details.
Accept: `tests/extensions/test_ext_race_report.py` rewritten one for one against the new file
(each old assertion has a new one or a written reason); `race.html` under 1.2 MB; no Plotly in
the file.

**B-34 Migrate the digital-twin run report (`src/twinflow/report/html.py` `HtmlReport`).** Opus.
`twinflow run` and `twinflow report` write the run viewer through `run_view_model` + `render`.
Feature inventory: Assumptions -> Details; Headline chips -> hero figures; Confidence ranges ->
ranges on hero figures, order whiskers, Details table; Shared resources -> Choke point (labor
row) + Details; Material flow (Mermaid) -> `ChokeFlow`; Charts (utilization, waits, WIP, order
lateness) -> Choke point cards, `OrderRiskList`, WIP line in Details; Detail tables (completion
and lateness by order, utilization, idle, machine hours, labor hours) -> Details, same numbers.
Keep `kpis.json`, `intervals.json`, `run_meta.json` unchanged.
Accept: every item in the inventory is checked off in the PR; existing report tests pass or are
replaced one for one; the report for `examples/cnc-shop` matches the mockup; `plotly_script()`
and `mermaid_script()` have no callers left and are removed with their vendored files.

**B-35 Migrate sweep and optimize output.** Sonnet.
`twinflow balance` and `twinflow optimize` write `sweep.html`: hero "What one change would help
most?", `LeverList`, a points table in Details. They also link to the base run so its run viewer
shows the levers.
Accept: cnc-shop sweep shows "Add a second shaft mill, 87% -> 99% on time" first.

**B-36 Migrate workspace result pages.** Sonnet.
`ExperimentView`, `DomainResults`, `ScenarioView` results, `DecisionLab`, `ActionReview` move
onto `Page`, `Section`, `Card`, `DecisionHero`, `Delta`, `StatusPill` and the chart parts. Each
page gets an answer hero and question titles. No behaviour change.
Accept: per page, a before and after screenshot pair and a short list of what moved where;
existing `web/tests/browser/workspace.spec.ts` passes; duplicate CSS for cards, pills and tables
in `workspace.css` is deleted in favour of the shared parts.

**B-37 Retire legacy tabs (`web/src/LegacyApp.tsx`, `RunTab`, `SweepTab`, `OptimizeTab`,
`FloorTab`, `ConfidenceBand`, `FulfillmentCards`, `UtilizationList`).** Sonnet.
Route them to the run viewer and sweep view. Delete the old components once nothing imports
them. `FloorMap` stays and takes the shared tokens.
Accept: no import of the retired files remains; the legacy URL redirects to the new page.

**B-38 Notebook and Python chart theme.** Sonnet.
`twinflow.report.theme` reads `tokens.json` and sets Matplotlib and Plotly defaults (colours,
fonts, grid, question as title helper). `twinflow.report.decision.show(view_model)` displays a
view inline in Jupyter. Rebuild `examples/molding-press/notebooks` with it.
Accept: notebooks run top to bottom in CI fast mode; chart colours equal the token values (unit
test on the theme); one notebook cell shows the race view inline.

**B-39 Docs figures follow the tokens.** Haiku, reviewed by Sonnet.
Cross-check `docs/design/VISUALS.md` (branch `feat/TF-006-visuals`) against `tokens.json`. List
and fix any colour or font that differs.
Accept: a script reports zero colours in docs SVGs that are not in `tokens.json`.

## Phase G. Copy and docs

**B-40 Copy pass.** Haiku, reviewed by Sonnet.
Every string in the views and view models against the word list in `DESIGN.md` 8.5. Reading
level check. No em dashes.
Accept: automated test (no em dash, banned-word list) passes; a reading-level script reports
grade 6 or lower for each sentence builder's output.

**B-41 Docs.** Haiku.
Update `docs/cli.md`, `docs/frontend.md`, `web/README.md` and the example READMEs: how to open
the new reports, what each section answers, the `--html` flags.
Accept: commands in the docs run as written.

**B-42 Full gate.** Opus.
Full Python suite, ruff, format, mypy, `npm run build`, `build:report`, Playwright, axe, size,
offline. Record results in the spec README. Feeds T-256.

---

## Order and parallel work

```
A: B-01 -> B-02 -> (B-03 | B-04) ; B-05 in parallel with all of A ; B-06 -> B-07
B: B-08 -> B-09 ; (B-10 | B-11 | B-12 | B-13) -> B-14
C: B-15 ; B-16 -> B-17 ; B-18 -> B-19 ; B-20 -> B-21 ; (B-22 | B-23 | B-24) -> B-25
D: B-26 -> B-27 ; (B-28 | B-29 | B-30)
E: B-31 -> B-32
F: B-33 after B-25 ; B-34 after B-27 ; B-35 after B-27 ; B-36, B-37 after B-32 ; B-38, B-39 after B-01
G: B-40 -> B-41 -> B-42
```

Phases B and C need B-05 for real data. Until it lands, front-end tasks use
`race-view-model.example.json` plus a hand-made fixture for the new fields.

## The five tasks that matter most

1. **B-05** race view-model gaps. Without the baseline's moves, the paired timing and the
   cautions, the honest parts of the design cannot be drawn.
2. **B-02** offline build pipeline. Every view and every migration depends on the single file.
3. **B-08 + B-09** the decision hero with run dots. This is the first screen and the point of
   the whole brief.
4. **B-18 + B-19** floor replay. The view planners will trust, because it looks like their floor.
5. **B-20 + B-21** state map. The hardest interactive piece, and the one that replaces the
   unreadable Markov chart.

Next in line: **B-34**, the migration of the digital-twin run report, since the owner made this
layout the standard for all reports.
