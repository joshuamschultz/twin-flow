# Build a twin

This guide starts a new twin folder from a ready-made starter.

## What this does

It makes a new folder with a plant file, two rule files and a short README.

## Before you start

- twinflow is installed. See [install](../install.md).
- Open a terminal in the folder where the new twin should live.
- You do not need to write code. You only read and copy short files.

## Steps

1. See the starter templates.

   ```bash
   twinflow new --list
   ```

   You will see:

   ```
   make-to-stock  One molding press, 12 products. Race rules for what to make next.
   job-shop       A CNC shop with state capture on and a dispatch rule file.
   custom-engine  A toy bakery written in Python. Copy it to model your own world.
   ```

2. Make the new folder from the make-to-stock starter. Use a short name with no spaces.

   ```bash
   twinflow new my-plant --template make-to-stock
   ```

   You will see:

   ```
   Created my-plant from the make-to-stock template.
   Next: twinflow extensions check my-plant
   ```

3. Open the new folder and look inside. It holds these files:

   ```
   my-plant/
     README.md                  the starter's notes, in plain words
     my-plant.twin.yaml         the plant: products, demand, money
     policies/
       family_campaign.py       a rule that stays with one color
       lowest_stock_first.py    a rule that makes what runs out first
   ```

## What is in the folder

- The `.twin.yaml` file is the plant. It is named after your folder. This is the file you edit.
- The `policies/` folder holds rules. A rule is a short file that picks what to make next. See [write a policy](write-a-policy.md).
- The `README.md` explains the starter in plain words.

## What you get

- A new folder with three files and a `policies/` folder.
- Nothing has run yet.
- The numbers in the plant file are sample numbers. Change them to match your plant.
- The `job-shop` starter is a machine shop. The `custom-engine` starter is a small world you write in Python.

## If something goes wrong

- **"already exists and is not empty"**: The folder name is taken. Pick a new name, or empty the folder. Nothing was changed.
- **"is not a template"**: The template name is wrong. Run `twinflow new --list` and copy a name from the list.
- **"new needs --template"**: You left out the template. Add `--template` and one name from the list.

## Next

[Load your data and economics](load-data-and-economics.md): enter your own plant numbers.
