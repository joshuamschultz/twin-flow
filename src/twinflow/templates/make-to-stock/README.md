# Make-to-stock starter

One molding press makes 12 products. Every day you pick one product to make,
or you let the press rest. Customers buy a random amount each day.
The question: which rule for "what do I make next" earns the most money?

## What is in this folder

- `molding-press.twin.yaml` is the plant: the press, the 12 products, the money.
- `policies/lowest_stock_first.py` makes the product that will run out first.
- `policies/family_campaign.py` stays with one color until it is stocked up.

Each rule is a short Python file. Open one, copy it, and change it.

## Try it

Check the files:

```bash
twinflow extensions check .
```

Race every rule on the same random demand and see who earns most:

```bash
twinflow policies race . --episodes 50
```

The race writes a folder under `runs/` with a `race.html` report. Open it in a browser.

Test a rule you wrote against the best one you have:

```bash
twinflow improve . --candidate my_rule.py
```

The rule is copied into `policies/` only if it wins by more than luck.

## Make it yours

Change the numbers in `molding-press.twin.yaml` to match your plant:
products, demand, run rates, prices and changeover costs.
