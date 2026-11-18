# Read the Markov map

This guide shows how to read a Markov map. The map shows how the plant moves from one kind of day to the next, and where the money goes.

## What this does

It takes the record of a run and draws a map of the states. It also shows which states the plant tends to stay in, and which moves are likely.

## Before you start

- You have a saved run folder from [run your twin](run-your-twin.md). For example, `press-out`.
- A make-to-stock run records its days on its own. A job shop needs state capture turned on.

## Words you need

- **State**: a short label that describes the plant on one day. For example, `grey:ok black:low natural:ok | grey` means the grey and natural families have enough stock, the black family is low, and the press is set up for grey.
- **Move**: one step from one state to the next state.
- **Sticky state**: a state the plant stays in at least half the time. The text list marks it with `(sticky)`.

## Steps

1. Make the map. Use the folder that the run saved to.

   ```bash
   twinflow markov press-out
   ```

   The command prints a short report and writes two files into the `press-out` folder: `markov.json` and `markov.mmd`. The `.mmd` file is a chart in Mermaid format.

   You will see something like this. It is a sample from a 2-run test:

   ```
   View: family
   Moves: 120
   Coverage: 83.3% of moves start in the 8 states below
   States:
      23.3%  grey:low black:low natural:low | grey
      13.3%  grey:low black:low natural:low | natural
   Likely moves (25):
      0.60  grey:low black:low natural:ok | natural  ->  grey:low black:low natural:ok | grey
      0.46  grey:low black:low natural:low | grey  ->  grey:low black:low natural:low | grey
   ```

2. Read the **States** list. Each percent is the share of days the plant spends in that state. The biggest share is the most common kind of day.

3. Read the **Likely moves** list. Each line has three parts: the chance of the move, the state it starts from, and the state it moves to. For example, `0.60` means a 60% chance of that move, from one state to the next. A move to the same state means the plant stays put.

4. Find the money leaks. A leak is a kind of day where money is lost.

   - Open `race.html` from a race. The section **Money leaks** lists the days where the best rules lose the most money in one run. It names the main cause, such as changeovers, keeping stock or lost sales.
   - Open `markov.json`. Under `money`, each state lists its visits, its revenue, and its costs by cause.
   - Run `twinflow states press-out` to see the total money by part.

5. Pick a view. A view is the kind of label the map uses. Add `--view` to change it.

   | View | What the label shows | Example |
   |---|---|---|
   | `full` | Every product's stock count, and the product set up | `GRY-S:480 GRY-M:320 ... \| GRY-S` |
   | `family` | The lowest level in each family, and the family set up. This is the default. | `grey:low black:low natural:ok \| grey` |
   | `simple` | Each family's level, as out, low, ok or high. This view comes from the press file. | `grey:out black:out natural:out \| black` |

   ```bash
   twinflow markov press-out --view simple --top 8
   ```

6. Adjust the size of the map if you need to. `--top 15` keeps the 15 most common states. `--min-prob 0.05` hides moves less likely than 5%.

## What you get

- The report in the terminal, with the states and the likely moves.
- `markov.json`: the full data, including the money for each state.
- `markov.mmd`: the chart in Mermaid format. Paste it into any Mermaid viewer to see the picture.

## If something goes wrong

- **"No view named 'x'"**: Use `family`, `full` or `simple`. You can also use a view name from `model.state_groups` in the plant file.
- **"No moves found"**: The run did not record any days. A job shop needs `state_capture` in the plant file. See [run your twin](run-your-twin.md).
- **"Coverage" is low**: The top states do not cover most days. Raise `--top`, for example to `--top 30`.
- **"does not exist or is not a folder"**: The folder name is wrong. Use the folder you gave to `--out` when you ran the scenario.

## Next

[Improve a policy](improve-a-policy.md): test a new rule against your best one.
