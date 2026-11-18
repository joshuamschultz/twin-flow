# twinflow decision views: data contract

Each view is drawn from one JSON object, its **view model**. Python builds it. The React
components only read it. The same object is served by the workspace API and inlined in the
offline HTML file (see `DESIGN.md` section 11).

Rules for every view model:

- Top level has `view` (`"race" | "run" | "states" | "improve" | "project"`), `schema_version`
  (integer, starts at 1), `about` (title, project, run facts, method in plain words),
  `agent_summary` (5 to 10 plain sentences), `warnings` and `problems`.
- Money is whole dollars. Shares are 0 to 1, four decimals. Day lists have one entry per day.
- Every sentence in the model is plain words, sixth-grade level, no em dashes. Sentences that
  state a result are built in Python, so the CLI, agents and the UI say the same thing.
- Ids are the machine names (`family_campaign`). `label` is the plain name ("Family campaign").
  The UI never shows an id when a label exists.
- Adding a field is not a breaking change. Removing or renaming one bumps `schema_version`.

Type notation below is TypeScript. `Stat` is `{mean, low, high, std?, n?}`.

---

## 1. Race view model

Built by `twinflow.extensions.report_model.race_view_model(result)` (T-269). The engineer's
real output is in `race-view-model.example.json` (short) and `race-view-model.full.json`. That
shape is accepted as the base. Section 1.2 lists what the design still needs.

### 1.1 Shape (existing fields, as built)

```ts
type RaceViewModel = {
  about: {
    title: string; project: string; episodes: number; seed: number; tune_episodes: number;
    horizon_days: number; score: string; score_is_dollars: boolean; interval: number;
    observation: string; view: string; policies_raced: number; method: string;
  };
  recommendation: {
    policy: string; label: string; settings: Record<string, number | string>;
    dollars_per_run: number; same_as_baseline: boolean; sentence: string; why: string;
  };
  baseline: { policy: string; label: string; source: "setting" | "first_policy_file" | "default";
              settings: object; rank: number; dollars_per_run: number };
  delta: { per_run: number; per_year: number; runs_per_year: number; low_per_run: number;
           high_per_run: number; low_per_year: number; high_per_year: number; assumption: string };
  confidence: { interval: number; mean: number; low: number; high: number; wins: number;
                runs: number; words: string; detail: string;
                level: "very_likely_better" | "maybe_better" | "maybe_worse"
                     | "very_likely_worse" | "same_policy" | "unknown" };
  floor: { key: string; label: string; unit: "count" | "share" | "units" | "days";
           recommended: number; baseline: number; change: number; better: boolean | null;
           words: string }[];
  drivers: { part: LedgerPart; gain: number; words: string }[];   // pick minus today, by part
  parts: LedgerPart[];             // "revenue" | "production" | "setup" | "holding" | "lost_sales" | "late"
  policies: PolicyRow[];
  products: { id: string; family: string; abc?: string; demand_rate: number;
              max_storage: number; low_stock: number }[];
  families: string[];
  example_runs: Record<string, ExampleRun>;   // keyed by policy id: the pick and the baseline
  markov: Record<string, MarkovView>;         // keyed by policy id
  leaks: Record<string, Leak[]>;
  tuning: { policy: string; chosen: object; tried: number;
            trials: { settings: object; score: number; dollars: number;
                      problem: string | null; chosen: boolean }[] }[];
  views: { observations: string[]; dollars: Record<string, Record<string, number>> };
  what_if: { parts: LedgerPart[]; words: Record<string, string>; min: number; max: number;
             step: number; note: string; formula: string };
  warnings: string[];
};

type PolicyRow = {
  policy: string; label: string; about: string; rank: number; source: "project" | "builtin";
  settings: object; is_baseline: boolean; is_recommended: boolean;
  score: Stat; dollars: Stat; vs_best: Stat | null; vs_baseline: Stat | null;
  parts: Record<LedgerPart, number>;
  fill_rate: number; changeovers: number; cross_family: number; idle_share: number;
  avg_stock: number; lost_units: number; dollar_warning: string | null;
  profit_by_day: { mean: number[]; low: number[]; high: number[] };   // cumulative, per day
};

type ExampleRun = {
  episode: number; start_stock: Record<string, number>;
  days: { day: number; action: string; setup: string; family: string | null;
          changeover: boolean; cross_family: boolean; stock: Record<string, number>;
          demand: number; sold: number; lost: number; profit: number; cumulative: number }[];
};

type MarkovView = {
  view: string; moves: number; coverage: number;
  nodes: { id: string; label: string;
           parts_of_label: { levels: Record<string, string>; setup: string | null } | null;
           share: number; visits: number; sticky: boolean; stay: number;
           per_day: number; per_run: number; leak_per_run: number;
           money_per_day: Record<LedgerPart, number>; losing: boolean; top_leak: boolean }[];
  edges: { from: string; to: string; prob: number; self: boolean }[];   // node ids
};

type Leak = { state: string; share: number; visits: number; per_visit: number; per_run: number;
              parts: Record<string, number>; cause: LedgerPart; cause_words: string };
```

Example (real values, shortened):

```json
{
  "recommendation": { "policy": "family_campaign", "label": "Family campaign",
    "settings": {"target_days": 16}, "dollars_per_run": 65410,
    "sentence": "Run family_campaign instead of lowest_cover. It makes $22,088 more per run, about $134,370 more a year." },
  "baseline": { "policy": "lowest_cover", "label": "Lowest cover", "source": "setting", "rank": 5, "dollars_per_run": 43322 },
  "delta": { "per_run": 22088, "per_year": 134370, "runs_per_year": 6.08, "low_per_run": 19733, "high_per_run": 24444 },
  "confidence": { "level": "very_likely_better", "words": "Very likely better", "wins": 40, "runs": 40 },
  "floor": [ { "key": "changeovers", "label": "Changeovers per run", "recommended": 45.3, "baseline": 59.0,
               "change": -13.7, "better": true, "words": "14 fewer changeovers" } ]
}
```

### 1.2 Mapping: design section to data to Python source

| Design section | Fields | Python source |
|---|---|---|
| Hero | `recommendation`, `baseline`, `delta`, `confidence`, `floor`, `cautions` (new) | `RaceResult.ranking()`, `PolicyResult.dollars/episode_dollars`, `stat()` in `extensions/race.py`; baseline from `ProjectConfig.race.baseline` |
| Run dots | `confidence.gaps` (new) | `PolicyResult.episode_dollars` of pick minus baseline, run by run |
| Why: waterfall | `drivers` | `PolicyResult.parts` (a `Ledger` per run, averaged) |
| Why: pull-ahead | `policies[].profit_by_day`, `timing` (new) | `RaceResult.cumulative()`, `band()` |
| All rules | `policies[].vs_baseline`, `rank`, `label`, `about`, `policies[].wins_vs_baseline` (new) | `_paired()` in `report_model.py` |
| Compare | all of `policies[]`, `policies[].dollars_by_run` (new) | `PolicyResult` |
| What if | `what_if`, `policies[].parts` | `Ledger` parts, `LEDGER_PARTS` in `state/records.py` |
| Replay | `example_runs`, `products`, `families`, `economics` (new) | `RaceResult.kept[policy]` (`MoveTable.frame`), `Move.before/after/events/ledger` |
| State map | `markov[policy]`, `views.levels` (new) | `ChainCounter.chain()`, `MarkovChain`, `StateMoney` in `state/markov.py`; `GroupedView` in `state/groups.py` |
| Money leaks | `leaks[policy]` | `money_leaks()` in `extensions/race.py` |
| Tuning | `tuning` | `RaceResult.trials` (`Trial`) |
| Details | `about`, `policies[]`, `warnings`, `problems` (new) | `RaceSettings`, `RaceResult.skipped` |

### 1.3 Gaps the engineers must add (race)

Ordered by how much the design depends on them.

| # | Field | Type | Why | Source |
|---|---|---|---|---|
| R1 | `view: "race"`, `schema_version: 1`, `generated_at`, `twinflow_version` | top level | One template serves five views. Old files must keep opening | constants |
| R2 | `confidence.gaps` | `number[]` (one per run, pick minus baseline) | The run dots in the hero | `episode_dollars` |
| R3 | `policies[].dollars_by_run` | `number[]` | Compare any two: runs won, and dots for any pair | `PolicyResult.episode_dollars` (rounded) |
| R4 | `policies[].wins_vs_baseline` | `number` | Leaderboard tooltip and Details ("Runs won") | paired count |
| R5 | `timing` | `{ gap_by_day: {mean: number[], low: number[], high: number[]}, pass_day: number \| null, lowest: {day: number, dollars: number}, words: string }` for pick vs baseline | "When does it pull ahead". The paired band cannot be derived from two separate bands | cumulative dollars per run, pick minus baseline, then `band()` |
| R6 | `cautions` | `{ key: "more_stock" \| "late_gain" \| "runs_short" \| "touchy_tuning" \| "end_stock" \| "idle" \| string; title: string; body: string; severity: "note" \| "warn" }[]`, most important first | The caution line and the three cards. Must be the same list in CLI, agents and UI | rules below |
| R7 | `policies[].stock_by_day` | `{mean: number[]}` (total units) and `stock_start`, `stock_end`, `stock_peak` | The "it does not win by emptying the shelf" check. `avg_stock` alone hides it | kept moves, or tallied while streaming for all policies |
| R8 | `policies[].out_share_by_product` | `Record<string, number>` (share of days with under 1 day of stock) | Caution "Natural products still run short" | tally per product while streaming |
| R9 | `example_runs`: pick the **typical** run, and use the **same** run for both rules | `episode` = the run whose gain is closest to the mean gain; add `why: "typical"` | Replay must show a normal run, on the same orders | `argmin(abs(gap - mean(gap)))` |
| R10 | `example_runs.*.days[].parts` | `Record<LedgerPart, number>` | Day sentence ("Colour change, $1,200") and Setup $ strip | `Move.ledger` |
| R11 | `example_runs.*.days[].lost_by_product` | `Record<string, number>` (only products with lost sales) | Day sentence and tooltips | `Move.events["lost.<id>"]` |
| R12 | Baseline moves are **always kept** | behaviour | Today the race keeps moves for the top 3 only (`top_policies`). The baseline is rank 5 here, so replay, state map and leaks for "today" would be empty | keep the baseline table in `race()` no matter its rank |
| R13 | `views.levels` | `{name: string; below: number \| null}[]` in order, plus `views.by` (`"days_of_cover"`) | Glyph bar heights and "low" colouring need the order of the levels | `StockGroups.levels` |
| R14 | `economics` | `{ changeover: {within_family: {cost, output_lost}, across_family: {cost, output_lost}}, holding_rate, lost_sale_rate }` | Copy such as "Those cost $1,200 each" | capsule `model.line.changeover`, `Economics` |
| R15 | `problems` | `{ policy?: string; file?: string; line?: number; message: string; fix: string }[]` | Details list and the quiet bar under the hero | `RaceResult.skipped` (`Problem`) |
| R16 | `agent_summary` | `string[]` | Agents, `<noscript>`, mail preview | built from the fields above |
| R17 | `tuning[].spread` and `tuning[].touchy` | `number`, `boolean` (spread above 25% of best) | Tuning table pill | trials |
| R18 | `markov[*].nodes[].words` | `{ title: string; setup: string }`, for example `{"title": "Grey high, natural low", "setup": "Press on black"}` | One wording everywhere. The front end can build it from `parts_of_label`, but leaks and agents need it too | `parts_of_label` |
| R19 | `leaks[*][].state_id` and `try` | node id; one plain sentence keyed on `cause` | Click a leak to select it in the map; "what to try" line | `money_leaks()` |
| R20 | `recommendation.sentence` uses labels | "Run Family campaign instead of Lowest cover..." | The sentence now shows ids (`family_campaign`) | `label` |

Caution rules (R6), first match first, at most three shown as cards:

| Key | When | Title and body (example) |
|---|---|---|
| `late_gain` | `timing.pass_day` is after 75% of the horizon | "The gain comes late." "The rule builds stock first. It is behind today's rule until day 54 of 60. Test a longer run before you commit." |
| `more_stock` | pick `avg_stock` at least 1.5 times baseline | "You will hold 2.6 times more stock." "About 3,400 units on the shelf, up from 1,300. Check the room and the cash first." |
| `runs_short` | any family's mean `out_share_by_product` above 10% | "Natural products still run short." "Each natural product is out about 1 day in 6." |
| `end_stock` | pick `stock_end` under 70% of baseline `stock_end` | "Part of the gain is sold-down stock." "It ends the run with less on the shelf than today's rule." |
| `touchy_tuning` | pick's tuning `touchy` is true | "This rule is touchy." "A wrong setting costs up to $X per run. Recheck it when demand changes." |
| `maybe` | `confidence.level` is `maybe_better` | "The lead is not proven." "It won 24 of 40 runs. Run more test runs." |

---

## 2. Run view model (job-shop simulation)

New. Proposed builder: `twinflow.report.decision.run_view_model(run_dir) -> dict`.

```ts
type RunViewModel = {
  view: "run"; schema_version: 1;
  about: { title: string; project: string; run_id: string; reps: number; seed: number;
           interval: number; time_unit: "minutes" | "hours" | "days"; horizon: number;
           method: string; assumptions: string[] };
  answer: { level: "all_on_time" | "most_on_time" | "many_late" | "unknown";
            headline: string;            // "Most will. Watch WO-9."
            lede: string;
            on_time: Stat;               // share 0..1 with range
            late_orders_per_run: number;
            watch: string[];             // order ids, most at risk first
            choke: { centre: string; label: string; busy: number } | null };
  orders: { id: string; part: string; qty: number; release: number; due: number | null;
            finish: Stat; room: number;          // due minus mean finish, in time_unit
            late_chance: number | null;          // share of reps finished after due
            status: "ok" | "risk" | "late" | "unfinished";
            reason: string;                      // "It waits in line at the shaft mill."
            steps?: { centre: string; label: string; work: number; wait: number }[] }[];
  centres: { id: string; label: string; busy: Stat; blocked: number; starved: number;
             queue_wait: number;               // mean wait per job, in time_unit
             status: "choke" | "next" | "ok" }[];
  routes: { part: string; steps: string[] }[];   // centre ids in order
  levers: { id: string; label: string; tested: boolean; on_time: number | null;
            gain_points: number | null; late_orders_per_run: number | null;
            note: string; command?: string }[];  // best first
  wip_by_time?: { t: number[]; by_centre: Record<string, number[]> };
  labor?: { pool: string; skill: string; hours: number }[];
  agent_summary: string[]; warnings: string[]; problems: Problem[];
};
```

Example (real values from cnc-shop, 30 reps):

```json
{
  "answer": { "level": "most_on_time", "headline": "Most will. Watch WO-9.",
              "on_time": {"mean": 0.873, "low": 0.833, "high": 0.913}, "late_orders_per_run": 1.3,
              "watch": ["wo-9", "wo-5", "wo-7"],
              "choke": {"centre": "mill_shaft", "label": "Shaft mill", "busy": 0.987} },
  "orders": [ { "id": "wo-9", "part": "shaft", "qty": 20, "release": 20.0, "due": 58.3,
                "finish": {"mean": 59.4, "low": 57.7, "high": 61.2}, "room": -1.1,
                "late_chance": 0.58, "status": "late", "reason": "It waits in line at the shaft mill." } ],
  "levers": [ { "id": "locations[mill_shaft].capacity=2", "label": "Add a second shaft mill",
                "tested": true, "on_time": 0.987, "gain_points": 11.4, "late_orders_per_run": 0.1,
                "note": "Late orders drop from 1.3 to 0.1 per run." } ]
}
```

### 2.1 Mapping and gaps (run)

| Field | Existing source | Gap |
|---|---|---|
| `answer.on_time`, `orders[].finish`, `centres[].busy` | `intervals.json` (`on_time_pct`, `completion_by_order`, `utilization_by_cell`) | none |
| `orders[].room` | `intervals.json` `lateness_by_order` (sign flipped) | none |
| `orders[].release`, `due`, `part`, `qty` | the plan (`plan.csv`) | **G1** copy the plan rows into the run folder (`plan.json`) so a report can be rebuilt from the run alone |
| `orders[].late_chance` | not stored | **G2** per-rep lateness by order (`lateness_by_order_by_rep`) or the share itself. The mockup estimates it from the range |
| `orders[].reason`, `orders[].steps` | event tables in the run folder | **G3** per order, time at each centre split into work and wait; reason = the centre with the longest wait |
| `centres[].blocked`, `starved` | `KpiSet.wait_seconds_by_location` (used by `HtmlReport._wait_figure`) | none |
| `centres[].queue_wait` | event tables | **G4** mean queue wait per job per centre |
| `centres[].status`, `answer.choke` | `HtmlReport._bottleneck(kpis)` | rule: `choke` is the busiest centre at or above 95%; `next` is at or above 75% |
| `routes` | `CompiledModel` routings (the Mermaid flow in `html.py` already walks them) | **G5** export as plain lists |
| `levers` | `summary.json` from `twinflow balance` / `optimize` | **G6** link a sweep to its base run (`base_run_id`), add `late_orders_per_run` per point, and a plain `label` per lever value ("Add a second shaft mill"); label template per lever path |
| `about.time_unit` | model clock is seconds | **G7** a display unit chosen from the horizon (under 3 hours: minutes; under 3 days: hours; else days), or `experiment.report.time_unit` |
| `answer.headline`, `lede`, `agent_summary` | none | **G8** sentence builder |
| labels for centres and parts | ids only (`mill_shaft`) | **G9** optional `label` on locations and parts in the model; fallback: id with underscores as spaces |

---

## 3. States view model

Proposed builder: `twinflow.report.decision.states_view_model(run_dir, view, top, min_prob)`.
It reuses the race `MarkovView` shape so `StateMap` has one input type.

```ts
type StatesViewModel = {
  view: "states"; schema_version: 1;
  about: { title: string; project: string; runs: number; episodes: number; moves: number;
           time_span: { from: number; to: number; unit: string }; states_seen: number;
           view_name: string; views_available: string[]; top: number; min_prob: number; note?: string };
  headline: string;        // "The plant spends 46% of its days with all stock low."
  avg_per_period: number;  // money on a normal period
  money: Record<LedgerPart, number>;        // totals per run
  chain: MarkovView;                        // nodes, edges, coverage (same shape as race)
  levels: { name: string; below: number | null }[] | null;
  families: string[] | null;
  leaks: Leak[];
  agent_summary: string[]; warnings: string[]; problems: Problem[];
};
```

Mapping: `twinflow states` / `twinflow markov` already compute everything (`read_moves`,
`estimate()`, `MarkovChain.to_json()`, `ChainCounter`, money by part).

Gaps:

- **S1** `MarkovChain.to_json()` gives edges by label and money in a separate map. Emit the
  race `MarkovView` shape (ids, `stay`, `per_day`, `money_per_day`, `parts_of_label`, `words`).
  Move that code from `report_model.py` to one shared function.
- **S2** `--html` on `twinflow states` and `twinflow markov` (T-271).
- **S3** `views_available` and `levels` from the capsule beside the run (`scenario.json`).
- **S4** `headline` sentence builder.

---

## 4. Improve view model

Built from `improve.json` (written by `twinflow improve`) plus the race view model next to it.

```ts
type ImproveViewModel = {
  view: "improve"; schema_version: 1;
  about: { title: string; project: string; episodes: number; seed: number; interval: number;
           rule: "interval_above_zero" | "mean_above_zero"; written_at: string };
  outcome: { promoted: boolean; level: "won" | "ahead_not_proven" | "lost" | "broken";
             headline: string; reason: string; copied_to: string | null };
  candidate: { policy: string; label: string; file: string; sha256: string; settings: object;
               rank: number; dollars: Stat; score: Stat };
  previous_best: { policy: string; label: string; settings: object; rank: number;
                   dollars: Stat; score: Stat };
  versus: { dollars: Stat; score: Stat; wins: number; runs: number; gaps: number[] };
  confidence: RaceViewModel["confidence"];
  floor: RaceViewModel["floor"];           // candidate vs previous best
  checks: { key: "ranked_first" | "proven" | "same_orders" | "no_problems";
            label: string; pass: boolean; detail: string }[];
  race_ref: string;                        // relative path or id of the full race view
  agent_summary: string[]; warnings: string[]; problems: Problem[];
};
```

Mapping: `improve.json` fields `promoted`, `reason`, `rule`, `interval`, `episodes`, `seed`,
`candidate`, `previous_best`, `versus`, `ranking`, `copied_to`, `written_at`
(`src/twinflow/cli/improve.py`).

Gaps: **I1** `versus.wins` and `versus.gaps`. **I2** `outcome.level` and `headline`.
**I3** `checks[]`. **I4** `floor` for candidate vs previous best (reuse the race builder with the
previous best as baseline). **I5** `labels`. **I6** `improve.html` output.

---

## 5. Project view model

Proposed builder: `twinflow.report.decision.project_view_model(project)`.

```ts
type ProjectViewModel = {
  view: "project"; schema_version: 1;
  about: { title: string; project: string; folder: string; capsule: string; domain: string;
           summary: string };            // "One molding press, 12 products in 3 families. Each run is 60 days."
  status: { level: "ready" | "needs_fixes" | "broken"; headline: string;
            errors: number; warnings: number };
  twin: { lines: { id: string; label: string }[]; products: number;
          families: { name: string; products: number }[]; horizon: { value: number; unit: string };
          costs: { label: string; value: string }[];      // "Colour change" -> "$1,200 and half a day"
          assumptions: string[]; provenance: string | null };
  rules: { policy: string; label: string; about: string; source: "project" | "builtin";
           file: string | null; is_baseline: boolean; settings_tried: number; ok: boolean }[];
  score: { name: string; is_dollars: boolean; file: string | null };
  views: { name: string; kind: "state_view" | "observation"; about: string }[];
  extensions: { kind: string; name: string; file: string; ok: boolean }[];   // engines, rewards, features
  problems: { severity: "error" | "warning"; where: string; line?: number;
              message: string; fix: string }[];           // errors first
  recent: { view: "race" | "run" | "improve"; id: string; when: string; sentence: string }[];
  agent_summary: string[];
};
```

Mapping: `Project.open()`, `ProjectConfig` (`race`, `markov`, `observations`, `score`),
`load_project()` in `extensions/loader.py` (`Extensions`), `checker.py`, `Issue`
(`path`, `message`, `suggestion`), the capsule (`model.products`, `line.changeover`,
`economics`, `assumptions`, `provenance`), `twinflow extensions list|check`.

Gaps: **P1** the builder itself. **P2** `about.summary` and cost sentences. **P3** `recent`
needs a small index of runs under `<project>/runs/` (id, kind, time, headline sentence).
**P4** `--html` on `twinflow extensions check`.

---

## 6. Shared types

```ts
type Stat = { mean: number; low: number; high: number; std?: number; n?: number };
type LedgerPart = "revenue" | "production" | "setup" | "holding" | "lost_sales" | "late";
type Problem = { where?: string; line?: number; message: string; fix: string };
```

Plain words for ledger parts (one table, used everywhere): revenue "sales", production "making
product", setup "changeovers", holding "keeping stock", lost_sales "lost sales", late "late
orders".

## 7. API (workspace)

| Call | Returns |
|---|---|
| `GET /api/v1/decision/races` | `{ id, project, when, sentence, level }[]` |
| `GET /api/v1/decision/race/{id}` | `RaceViewModel` |
| `GET /api/v1/decision/runs`, `/run/{id}` | list, `RunViewModel` |
| `GET /api/v1/decision/states/{id}?view=&top=&min_prob=` | `StatesViewModel` |
| `GET /api/v1/decision/improve/{id}` | `ImproveViewModel` |
| `GET /api/v1/decision/project/{id}` | `ProjectViewModel` |
| `GET /api/v1/decision/{view}/{id}/export` | the offline HTML file |

## 8. Tests the contract needs

- A JSON Schema per view model in `src/twinflow/report/schemas/`. Python tests validate real
  output against it. TypeScript types are generated from the same schema, so the two sides cannot
  drift.
- Golden files: `race-view-model.example.json` stays the fixture for front-end tests.
- A test that every user-facing string in a view model has no em dash and no raw policy id where
  a label exists.
