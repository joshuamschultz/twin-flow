# Custom engine starter

Your own world, written in Python. This one is a tiny bakery with two breads.
Each day the baker bakes one bread or rests. Customers buy a random amount.

## What is in this folder

- `engines/bakery.py` is the world. It is about 60 lines with comments. Copy it and change it.
- `bakery.twin.yaml` is the capsule. `model.engine: bakery` picks the engine file.
- `policies/bake_the_lowest.py` is a rule that bakes the bread that will run out first.

## Try it

Check the files:

```bash
twinflow extensions check .
```

Race the rules on the same random days:

```bash
twinflow policies race . --episodes 50
```

## Make it yours

An engine needs four methods: `start`, `actions`, `step` and `done`.
Every `step` returns one move with the dollars split by cause.
Change the numbers, then the rules in `step`, then rename the engine.
