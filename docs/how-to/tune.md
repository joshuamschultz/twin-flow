# Tune your twin

<!-- Made from twinflow.extensions.project.DEFAULTS. Do not edit by hand: a test
     compares this page with the table. -->

Every number and choice below is set in the capsule's `experiment:` section.
Leave a setting out and the default is used. A misspelled setting is reported
with the closest real name.

```yaml
experiment:
  score: dollars
  race: {episodes: 200, seed: 0}
  checker: {time_limit_ms: 20}
```

| Setting | Default | What it does |
|---|---|---|
| `experiment.score` | `dollars` | The reward file the race ranks policies by. Dollars are always shown next to it. |
| `experiment.demand` | `poisson` | The demand file that draws each day's customer demand. Draws are made once per seed, so every policy sees the same dice. |
| `experiment.features` | `[]` | Feature files whose values every policy state gets (state.features) and every recorded state keeps (extra). |
| `experiment.observation` | `[full]` | What policies may see: full, no_setup, bucketed, no_days_left, own_product, or an observation file. Join masks with + (bucketed+no_days_left). A list races every policy under each one. |
| `experiment.race.episodes` | `200` | Runs per policy in the race. Every policy runs on the same demand draws. |
| `experiment.race.seed` | `0` | Seed of the race's demand draws. |
| `experiment.race.tune_episodes` | `30` | Runs per trial setting when the race tunes a policy's SETTINGS. |
| `experiment.race.tune_seed` | `777` | Seed of the tuning draws. It differs from the race seed, so settings are not picked to fit the scoring dice. |
| `experiment.race.interval` | `0.95` | How sure the intervals in the race report are (0.95 means 95%). |
| `experiment.race.promote_rule` | `interval_above_zero` | When `twinflow improve` keeps a candidate: interval_above_zero (the paired interval against the best is above zero) or mean_above_zero (it wins on average). |
| `experiment.race.top_policies` | `3` | How many of the best policies get a Markov map and saved moves. |
| `experiment.race.baseline` | `first policy file, else lowest_cover` | The policy that stands for what the plant does today. The race report shows every number as a change from it. |
| `experiment.race.decision_timeout_s` | `2.0` | Seconds one decision may take in the race before the policy is stopped as slow. |
| `experiment.checker.time_limit_ms` | `20.0` | Average milliseconds a decision (or any extension call) may take in the check. |
| `experiment.checker.samples` | `200` | Most sample states the check tries each file on. |
| `experiment.markov.view` | `family` | The view the race's Markov maps and money leaks use. |
| `experiment.markov.top_k` | `15` | How many of the most visited states a Markov map shows. |
| `experiment.markov.min_prob` | `0.05` | Moves less likely than this are left off a Markov map. |

To change how a policy, reward, view, feature, demand or observation file works,
copy the built-in file into the matching folder of your project and edit it. A file
with the same `NAME` as a built-in replaces it.
