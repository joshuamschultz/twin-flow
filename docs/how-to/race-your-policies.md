# Race your policies

This guide runs every rule on the same random demand. Then it ranks the rules by how much money they make.

## What this does

It plays each rule in your `policies/` folder, plus the built-in rules, over many runs. Then it ranks them and writes a report.

## Before you start

- You have a plant folder with a `policies/` folder. See [build a twin](build-a-twin.md).
- You have checked the files. See [check your twin](check-your-twin.md).

## Steps

1. See the rules the race will use.

   ```bash
   twinflow policies list my-plant
   ```

   You see one row for each rule:

   ```
   name                source   decision   settings                                 problems
   abc_wheel           builtin  make_next  days_per_slot                            0
   family_campaign     project  make_next  target_days                              0
   lowest_stock_first  project  make_next  stop_days                                0
   ```

   The `source` column says `builtin` for the rules that come with twinflow, and `project` for your own.

2. Check the rules once more.

   ```bash
   twinflow policies check my-plant
   ```

   You see `No problems found.` when all the rules are good.

3. Race the rules over 20 runs each. Each run covers 60 days.

   ```bash
   twinflow policies race my-plant --episodes 20
   ```

   The command prints one progress line for each rule. Then it prints a ranking table, and the path to the report:

   ```
   Rank  Policy              Score    Dollars per run                          Fill rate  Changeovers
   1     family_campaign     64,803   $64,803 (95% range 62,992 to 66,613)     96.7%      45.4
   2     s_big_s             56,562   $56,562 (95% range 54,912 to 58,212)     94.4%      27.9
   3     setup_aware_runout  51,185   $51,185 (95% range 49,635 to 52,736)     98.7%      58.8
   ...
   Report: my-plant/runs/race-20261118T054819Z/race.html
   ```

   Without `--episodes`, the race runs 200 times per rule. That is the setting `experiment.race.episodes` in the plant file.

4. Open the report. Go to the folder named in the last line, and open `race.html` in a web browser.

   The report has these parts:

   - **The winner**: the top rule and its average dollars per run.
   - **Ranking**: the same table as the command, with settings and the gap to the winner.
   - **Money leaks**: the days when the best rules lose the most money, and the main cause.
   - **Profit by view**: the profit of each rule when it sees the full plant.
   - **Markov maps**: how the top rules move from one kind of day to another. See [read the Markov map](read-the-markov-map.md).

## How to read the table

- **Rank**: 1 is the best rule.
- **Score**: the number the race sorts by. By default, this is dollars.
- **Dollars per run**: the average profit of one run, with a 95% range. The range shows where the true average most likely sits. If two ranges overlap, the gap may be luck.
- **Fill rate**: the share of customer demand you filled.
- **Changeovers**: how many times the press switched products in one run.

## What "same random demand" means

Every rule gets the same customer orders in each run. The race draws the random demand once for each run number. Then every rule plays that same set of draws. So if one rule wins, it won on the same orders as the others. It did not get lucky orders.

Change `--seed` to get a different set of draws. Do not pick a seed to make one rule win.

## Dollars and score

The race ranks rules by score. The default score is dollars, so both columns match. If you change the score, the two can disagree. When that happens, the race prints a warning like this:

```
Warning: kanban beats s_big_s on the score but makes $12,267 less per run.
```

Read the warning before you pick a winner. A rule that wins on score can still lose money.

## What you get

- A folder under `runs/` in the plant folder, named `race-` and a time stamp.
- `race.json`: the numbers for every rule.
- `race.html`: the report. Open it in a browser.

## If something goes wrong

- **"The model domain is None"**: The folder is not a make-to-stock plant or a custom engine. A job shop does not race. Use `twinflow run` for a job shop. See [test orders](test-orders.md).
- **"Skipped policies"**: A rule crashed, so the race left it out. The message names the file and the line. Fix that line. A built-in rule can be skipped in a custom engine, because it needs products the engine does not have.
- **"internal error" and exit code 2**: A setting in the plant file names a reward, view, feature or demand that does not exist. Run `twinflow extensions check my-plant` to find the name.
- **A rule you wrote is not in the ranking**: Run `twinflow policies list my-plant`. If the rule is not there, check that the file is in `policies/` and that `decide` is spelled correctly.

## Next

[Read the Markov map](read-the-markov-map.md): see how the plant moves from one state to the next.
