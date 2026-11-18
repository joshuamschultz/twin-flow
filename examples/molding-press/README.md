# Molding press example

One molding press makes 12 products. The products come in 3 colors:
grey, black and natural. Every day you pick one product to make, or you
let the press rest. Customers buy a random amount each day. If a product
is out of stock, the sale is lost.

The big question: **which rule for "what do I make next" earns the most money?**

## What is in this folder

- `molding-press.twin.yaml` is the plant. It lists the press, the 12 products, the money and the changeover costs.
- `policies/lowest_stock_first.py` is a rule written as a short Python function. It makes the product that will run out first.
- `policies/family_campaign.py` is another rule. It stays with one color until that color is stocked up. Switching color is expensive, so this saves money.

Open the two policy files. Each one is about 20 lines. You can copy one and change it.

## How the plant works

- A day has one choice: make one product, or rest.
- Switching to a product in the same color costs $200 and loses 10% of the day.
- Switching to a new color costs $1,200 and loses half of the day.
- The press cannot make more than a product's storage space allows.
- Keeping stock costs 1% of the unit cost each night.
- A lost sale costs 30% of the price.
- A run lasts 60 days. Each product starts with 8 days of stock.

The numbers come from the `rl-mfg-supplychain` research project.
The twin gives the same profit as that project for the same random demand.

## Run it

Check the file:

```bash
twinflow scenario validate examples/molding-press/molding-press.twin.yaml
```

Run 50 random 60-day runs with the built-in "lowest cover" rule:

```bash
twinflow scenario run examples/molding-press/molding-press.twin.yaml --reps 50 --seed 2024
```

The command prints the result and saves `result.json` in the new `runs/` folder.
Look at `mean_profit` under `metrics`. For this seed it is about $44,100 per 60 days.

To try another built-in rule, change `policy` in the `experiment` part of
the file. Good choices are `family_campaign`, `s_big_s`, `kanban`, `fixed_wheel` and `idle`.
With `family_campaign` the profit is about $65,100. That is about $21,000 more
than "lowest cover", only because the press changes color less often.

## The state map

The file has a short view named `simple`. It gives each day a label such as
`grey:ok black:ok natural:ok | grey`. It means: every color has an OK
amount of stock, and the press is set for grey. Later tools use these
labels to draw a map of how the plant moves from day to day.

## Race the rules and open the reports

Race every built-in rule and the two rule files on the same random demand:

```bash
twinflow policies race examples/molding-press --episodes 20 --out runs/race
```

Open `runs/race/race.html` in a web browser. It answers two questions: which rule earns the
most, and is the gain real? The table in the terminal shows the same ranking.

Look at the state map of the best rule. Add `--html` to also write `states.html`:

```bash
twinflow states runs/race/policies/family_campaign --view simple --top 5 --html
```

Open `runs/race/policies/family_campaign/states.html`. It shows the situations the plant
lives in, and which ones cost money.

To check the rule files and the project, write `project.html` into the project folder:

```bash
twinflow extensions check examples/molding-press --html
```

Open `examples/molding-press/project.html`. It lists any problems first, then what the twin
holds and which rules can race.

## Test a new rule

`candidates/keep_stocked.py` is a rule to try. It is `lowest_stock_first` with a higher stop
point. `improve` races it against your current rules on the same demand:

```bash
twinflow improve examples/molding-press --candidate examples/molding-press/candidates/keep_stocked.py --out runs/improve
```

Open `runs/improve/improve.html`. It shows whether the new rule won, a checklist that says
whether the result can be trusted, and how the two rules did. The rule is copied into
`policies/` only when it wins. With the settings above, this rule does not win, so nothing is copied.

## Publish to the workspace

To see these races on the DECISIONS pages of the web app, publish one into a workspace folder:

```bash
twinflow policies race examples/molding-press --episodes 20 --workspace .twinflow-workspace
```

Then start the service with the same folder, and open DECISIONS > Rule races. See
[`docs/frontend.md`](../../docs/frontend.md).

## What this shows

The press itself is not the problem. The order of work is. A rule that
changes color all the time wastes money on setups. A rule that waits too
long loses sales. You can test each rule on the same random demand, so the
comparison is fair.

## More

- **For planners:** [`examples/showcase/plastics-molding/`](../showcase/plastics-molding/)
  is the same kind of plant as a business would set it up: a product table and sales
  history in their own files, and a notebook of commands with the decision in plain words.
- **For analysts:** two notebooks in [`notebooks/analyst/`](notebooks/analyst/) rebuild
  the research repo's lessons with twinflow's Python API.
