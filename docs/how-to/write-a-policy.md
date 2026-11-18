# Write a policy

![Three conveyor lanes meeting at one switch lever in front of a molding press](../images/headers/how-to-write-a-policy.webp)

This guide shows how to write a policy. A policy is a short file that picks what the press makes next.

## What this does

It shows the parts of a policy file, every name a policy can read, and how to copy a built-in rule and change it.

## Before you start

- You have a plant folder with a `policies/` folder. See [build a twin](build-a-twin.md).
- You can copy and edit a plain text file. You do not need to know Python well. The file is short.

## The parts of a policy file

A policy file must have one function called `decide`. It runs once for each day. It returns one answer: a product name from `state.actions`, or `"idle"` to rest the press.

Three names are optional:

- `NAME`: the name shown in race tables. If you leave it out, the file name is used.
- `DECISION`: `"make_next"` (the default) is for a press. `"dispatch"` is for a job shop. A dispatch file sees the waiting jobs in `state.queue`. See `busy_edd.py` in the job-shop starter.
- `SETTINGS`: a list of values the race can try, such as `{"stop_days": [4, 8, 12]}`. Each setting name must be an argument of `decide`.

## Steps

1. Make a file in the `policies/` folder. Give it a short name with no spaces, such as `run_out_first.py`. Type this:

   ```python
   """Make the product that will run out first."""

   NAME = "run_out_first"
   SETTINGS = {"stop_days": [4, 8, 12]}


   def decide(state, stop_days=8):
       neediest = min(state.products, key=lambda p: state.days_of_cover[p])
       if state.days_of_cover[neediest] >= stop_days:
           return "idle"
       return neediest
   ```

2. Check the file.

   ```bash
   twinflow policies check my-plant
   ```

3. Copy a built-in rule and change it. In a source checkout, the built-in rules are in `src/twinflow/extensions/builtin/policies/`. Copy `lowest_cover.py` into your `policies/` folder. Then open the copy and change the `NAME` to a new name, such as `my_cover`. Change the numbers in `SETTINGS` or the logic in `decide`.

4. Check the list of rules. Your copy shows as `project`.

   ```bash
   twinflow policies list my-plant
   ```

   If you keep the same `NAME` as a built-in rule, your copy replaces the built-in rule in the project.

## The names a policy can read

Each name below is a field of `state`. Use it as `state.stock`, for example. Most names hold one value for each product, and you read them as `state.stock["GRY-S"]`.

| Name | What it holds |
|---|---|
| `products` | The product names, in order. Loop over them with `for p in state.products`. |
| `stock` | Units on hand for each product. |
| `days_of_cover` | Days each product's stock lasts at its usual sales rate. |
| `demand_rate` | Average units sold per day for each product. |
| `family` | The family (such as a color) of each product. |
| `abc` | `A`, `B` or `C` for each product. |
| `price` | Sale price for one unit. |
| `unit_cost` | Cost to make one unit. |
| `margin` | Price minus unit cost. |
| `max_storage` | The most units of each product you can keep. |
| `run_rate` | Units made in a full day. |
| `holding_cost` | Cost of keeping one unit in stock for one day. |
| `lost_sale_cost` | Cost of one lost unit. |
| `changeover` | Switching costs. For example, `state.changeover["within_family"]["cost"]`. |
| `setup` | The product the press is set up for now. It can be `None`. |
| `period` | Today's day number. The first day is 0. |
| `horizon` | Days in one run. |
| `days_left` | Days left in the run. |
| `actions` | The answers `decide` may return: each product name, plus `"idle"`. |
| `features` | Extra numbers from feature files. See [the next guide](write-a-reward-view-feature-or-demand.md). |

## What you get

- After step 2, you see `No problems found.` when the file is good.
- After you race, your rule appears in the ranking. See [race your policies](race-your-policies.md).

## If something goes wrong

- **"decide returned 'X', which is not one of ..."**: The answer is not in `state.actions`. Return a product name from that list, or `"idle"`.
- **"decide raised KeyError"** or **"decide raised ZeroDivisionError"**: The code breaks for some input. The message names the line. Make that line handle the case, for example by checking for zero first.
- **"SETTINGS has 'stop_day', but decide has no setting with that name"**: The spelling differs between `SETTINGS` and the argument of `decide`. Make them match.
- **"DECISION is 'maybe'"**: Use `"make_next"` or `"dispatch"`.
- **A rule is missing from the race**: A rule that crashes is listed under "Skipped policies" in the race output. Fix the line the message names.

## Next

[Write a reward, view, feature or demand](write-a-reward-view-feature-or-demand.md): change how days are scored and labeled.
