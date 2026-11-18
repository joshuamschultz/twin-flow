# Check your twin

This guide shows how to find mistakes in your plant file and rule files before you run anything.

## What this does

It runs two checks. One checks the plant file. The other checks the rule files and the other add-on files.

## Before you start

- You have a plant file, from the starter or from [load your data](load-data-and-economics.md).

## Steps

1. Check the plant file. Give the file name, not the folder.

   ```bash
   twinflow scenario validate my-plant/my-plant.twin.yaml
   ```

   No message means the file passed.

   When there is a problem, you see one line for each problem. The line gives the path to the field and what is wrong:

   ```
   error: $.model.products[0].demnad: 'demnad' is not a known field here.
   error: $.model.products[0].demand: demand is missing.
   ```

2. Check the rule files and the other add-on files. Give the folder name.

   ```bash
   twinflow extensions check my-plant
   ```

   When everything is fine, you see:

   ```
   No problems found.
   ```

   When a rule has a problem, you see the file, the line and the fix:

   ```
   policies/bad_pick.py line 7: decide returned 'GRY-XXL', which is not one of: GRY-S, GRY-M, ... idle. Fix: Make decide return one of state.actions.
   policies/crash.py line 5: decide raised ZeroDivisionError: division by zero on the state for day 0. Fix: Fix line 5 so it works for every input, for example empty stock.
   ```

   The list ends with a count, such as "4 problem(s), 4 of them errors."

3. To see every file the checker found, run this command:

   ```bash
   twinflow extensions list my-plant
   ```

   It lists each file, where it came from, and how many problems it has.

## What you get

- Silence from step 1, or "No problems found." from step 2, means the files are ready to run.
- A list of problems means you must fix them first. Each problem names the file and the line.
- Both checks exit with an error code when they find an error. Use this when a script calls them.

## If something goes wrong

- **A rule name is wrong, and the run has no numbers.** Set `policy:` in the `experiment:` section to a name from `twinflow policies list my-plant`. Neither check catches a wrong policy name. The run then ends with no numbers and no error message.
- **A setting name has a typo, and `scenario validate` says nothing.** Run `twinflow extensions check my-plant` as well. It catches typos in `experiment:` settings and suggests the right name. For example, `replicatons` gets "Did you mean 'replications'?".
- **You pass a folder to `scenario validate`.** You see "Is a directory". Give the `.twin.yaml` file name instead.
- **You pass a `model.yaml` file.** You see "this file is a legacy floor model". A `model.yaml` is for the older commands in [test orders](test-orders.md). Use the `.twin.yaml` file here.

## Next

[Run your twin](run-your-twin.md): run the plant and save the results.
