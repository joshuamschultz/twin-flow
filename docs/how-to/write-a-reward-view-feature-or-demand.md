# Write a reward, view, feature or demand

This guide shows the other kinds of files you can add to a plant. For policy files, see [write a policy](write-a-policy.md).

## What this does

It shows one short example of each kind, and the setting in the plant file that picks it. The setting uses the file's `NAME`, not its file name.

## Before you start

- You have a plant folder and know how to write a policy. See [build a twin](build-a-twin.md) and [write a policy](write-a-policy.md).

## The five kinds

| Kind | Folder | Function | Setting in the plant file |
|---|---|---|---|
| Reward | `rewards/` | `reward(move)` | `experiment.score` |
| View | `views/` | `label(state)` | `experiment.markov.view` |
| Feature | `features/` | `features(state)` | `experiment.features` |
| Demand | `demand/` | `demand(period, rng, model)` | `experiment.demand` |
| Observation | `observations/` | `observe(state)` | `experiment.observation` |

## Steps

1. **Reward: how a day is scored.** Save this as `rewards/lost_sales_double.py`:

   ```python
   """Score each day by its dollars, with lost sales counted twice."""

   NAME = "lost_sales_double"


   def reward(move):
       return move.ledger.total - move.ledger.lost_sales
   ```

   Then set the score in the `experiment:` section:

   ```yaml
   experiment:
     score: lost_sales_double
   ```

   `move.ledger` has `revenue`, `production`, `setup`, `holding`, `lost_sales`, `late` and `total`.

2. **View: a short label for each day.** Save this as `views/any_low.py`:

   ```python
   """One word for the plant: is any product running low?"""

   NAME = "any_low"


   def label(state):
       low = any(state.days_of_cover[p] < 3 for p in state.products)
       return "some low" if low else "all ok"
   ```

   Then pick it for the race's Markov maps:

   ```yaml
   experiment:
     markov:
       view: any_low
   ```

   `twinflow states` and `twinflow markov` do not use view files. The race does.

3. **Feature: an extra number on every day.** The built-in file `features/cover_days.py` looks like this:

   ```python
   NAME = "cover_days"


   def features(state):
       return {f"cover_days.{p}": state.days_of_cover[p] for p in state.products}
   ```

   Turn it on with `features: [cover_days]`. A policy then reads the number as `state.features["cover_days.GRY-S"]`.

4. **Demand: how many units customers buy.** The built-in file `demand/promotion.py` sells more units on the days listed in its `SETTINGS`. Turn it on:

   ```yaml
   experiment:
     demand: promotion
   ```

5. **Observation: what a policy is allowed to see.** The built-in file `observations/no_days_left.py` hides the day number:

   ```python
   NAME = "no_days_left"


   def observe(state):
       return state.replace(period=-1)
   ```

   Turn it on with `observation: no_days_left`. `state.replace` can only change `stock`, `setup`, `period`, `actions` and `features`.

To change a built-in file, copy it from `src/twinflow/extensions/builtin/` into the folder of the same name in your plant folder. Then edit the copy and give it a new `NAME`.

## What you get

- Nothing prints when you edit a file. Run `twinflow extensions check my-plant` to test it.

## If something goes wrong

- **"experiment.score names 'dolars', but there is no reward with that name. Did you mean 'dollars'?"**: The setting does not match a `NAME`. Fix the spelling. The check suggests the closest name.
- **The race stops with "internal error" and exit code 2**: A setting names a file that does not exist. Run `twinflow extensions check my-plant` to find it.
- **The numbers do not change**: The file must be in the matching folder, and the setting must match its `NAME`.

## Next

[Race your policies](race-your-policies.md): test many rules on the same random demand.
