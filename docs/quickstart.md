# Quick start

From nothing to a running twin in the browser.

## 1. Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
```

## 2. Run a floor from the command line

```bash
twinflow validate examples/cnc-shop/model.yaml
twinflow run examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv --reps 30
```

Open the `report.html` the run prints. Every KPI comes back as a mean with a
low-to-high confidence band across the 30 replications.

## 3. Let an optimizer propose staffing

```bash
twinflow optimize examples/cnc-shop/model.yaml --plan examples/cnc-shop/plan.csv \
  --lever labor.pools[0].headcount:2:6 \
  --objective on_time_pct --optimizer hill_climb --budget 12 --reps 12
```

It prints the winning headcount, its on-time band, and how many evaluations it used.
The twin scores; the decision stays yours.

## 4. Drive it from Python

```python
from twinflow.model import load_model
from twinflow.plan.loader import load_plan
from twinflow.modules import (
    ScoringSurface,
    Scenario,
    LeverSpace,
    IntRange,
    OBJECTIVES,
    OPTIMIZERS,
    optimize,
)

model = "examples/cnc-shop/model.yaml"
plan = load_plan("examples/cnc-shop/plan.csv", load_model(model).registry)

result = optimize(
    model,
    plan,
    LeverSpace({"labor.pools[0].headcount": IntRange(2, 6)}),
    OBJECTIVES.create("robust_on_time"),
    OPTIMIZERS.create("hill_climb"),
    budget=12,
    reps=12,
)
print(result.best.scenario.levers, result.best_score)
```

## 5. Open the whole twin in the browser

```bash
pip install -e ".[api]"
twinflow serve --port 8000          # terminal 1
```

```bash
cd web && npm install && npm run dev   # terminal 2
```

Open the Vite URL. Pick a model, see its floor map, and run / sweep / optimize from the
tabs.

## Next

- [modeling.md](modeling.md) — build your own floor
- [modules.md](modules.md) — objectives, costs, optimizers, ML models, and how to add your own
- [cli.md](cli.md) · [api.md](api.md) · [frontend.md](frontend.md)
