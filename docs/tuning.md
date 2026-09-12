# Tuning and calibration

A twin is only useful if its numbers match your floor. This guide covers setting how
*variable* the floor is, making the twin **reproduce your real history** (calibration),
choosing what to tune, and the honest limits of the defaults. It assumes you can run a
twin ([running.md](running.md)).

## 1. Variation — how random is your floor

Real cycle times are not exact. `cv` (coefficient of variation) says how much they wobble.

```yaml
defaults:
  cycle_time_cv: 0.2        # every cycle time gets a +/-20% lognormal spread
```

You can also set `cv` on a single time model to override the default there. What it does:

- `cv: 0` — deterministic. Every replication is identical, every band is a point. Use only
  for testing.
- `cv: 0.1` — steady floor. Narrow bands.
- `cv: 0.3+` — jumpy floor. Wide bands.

**The `cv` controls the width of your confidence ranges.** If your real floor swings more
than the twin shows, raise `cv`; if the twin looks noisier than reality, lower it. Getting
`cv` right is the difference between a band you can trust and one that is too tight (false
confidence) or too loose (useless).

Rule of thumb starting points: machining/assembly `~0.15`, manual or mixed work `~0.25`,
highly variable or new processes `~0.35`.

## 2. Replications — how many runs

Variation only shows up across replications. Set `--reps` high enough that the band is
stable:

- **20-30** is a good default.
- If the band still shifts noticeably when you re-run, raise `--reps`.
- `--reps 1` hides all variation. Never trust a one-rep number.

## 3. Calibration — reproduce your real history

This is the step that earns trust: *"the twin reproduced my last 8 weeks within 5%."*

Calibration is just optimization with a twist: instead of maximizing on-time, it **tunes
model parameters until the simulated KPIs match the observed ones**. The parameters you
tune are ordinary levers (`cv`, a rate, a scrap fraction, a reorder point), so it reuses
the same surface and optimizers as everything else.

### The workflow

1. Get your real production event log into the twin's event schema and compute its KPIs
   (this is the "observed" target). If you have a parquet event log, read it and run the
   shipped `compute_kpis` over it, or read a prior twin run's own KPIs.
2. Declare which parameters to tune and their ranges (`LeverSpace`).
3. Declare which KPIs must match, and how much each matters (`weights`).
4. Run `calibrate`. It returns the best-fit parameters, the distance to your history, and
   whether it met your tolerance.

### The API

```python
from twinflow.model import load_model
from twinflow.plan.loader import load_plan
from twinflow.modules import (
    LeverSpace,
    IntRange,
    OPTIMIZERS,
    CalibrationTarget,
    calibrate,
)
from twinflow.instrumentation import compute_kpis

model = "model.yaml"
plan = load_plan("plan.csv", load_model(model).registry)

# 1. observed KPIs from your real event log (same schema as the twin's own)
observed = compute_kpis("real_last_8_weeks.parquet", orders_frame, horizon)

# 2 + 3. what to tune, and what must match (weights say which KPIs matter)
target = CalibrationTarget(
    observed=observed,
    weights={"on_time_pct": 1.0, "run_hours": 0.3},
)

# 4. tune the spread until the twin matches history
result = calibrate(
    model,
    plan,
    LeverSpace(
        {"defaults.cycle_time_cv": IntRange(0, 40)}
    ),  # searched as 0.00 .. 0.40 if you map it
    target,
    OPTIMIZERS.create("hill_climb"),
    budget=15,
    tolerance=5.0,
    reps=20,
)

print("best-fit params:", result.best_params)
print("distance to history:", round(result.distance, 2))
print("within tolerance:", result.within_tolerance)
```

`CalibrationResult` gives you:

- **`best_params`** — the parameter values that best reproduced your floor.
- **`distance`** — the weighted gap between simulated and observed KPIs (0 = perfect).
- **`within_tolerance`** — did it meet the `tolerance` you set.
- **`search`** — the full optimization trail, if you want to inspect it.

### Which KPIs can I match on?

Set any of these in `weights` (a weight is how much that KPI matters):

`on_time_pct`, `run_hours`, `setup_hours` (scalars), and `utilization_by_cell`,
`machine_hours_by_machine`, `lateness_by_order`, `completion_by_order` (matched as the
average gap across their entries).

### Reading the fit honestly

- A small `distance` within `tolerance` means the twin reproduces your floor. Now its
  predictions are believable, and an optimum found on it is trustworthy.
- A large `distance` means the model is missing something real (a disruption, a routing, a
  constraint). Do not tune your way past it — fix the model, then recalibrate.

## 4. Choosing what to tune

Do not tune everything. Tune the few parameters you are least sure of and that move the
KPIs you care about:

- **Unsure of variability?** Tune `cv`.
- **Unsure of a machine's real speed?** Tune that location's `rate`.
- **Unsure of scrap or yield?** Tune the `scrap.rate`.
- **Unsure of inventory policy?** Tune `reorder_point` / `refill_to`.

## 5. Sensitivity — find what matters before you tune

Before calibrating, a quick sweep tells you which levers actually move the outcome, so you
tune the ones that matter and ignore the ones that do not:

```bash
twinflow balance model.yaml --plan plan.csv --sweep sweep.json --reps 20
```

If a lever barely changes the KPIs across its whole range, it is not worth tuning (or
optimizing). If a small change swings the band, that is where your attention belongs.

## 6. The honest guardrails (what the twin assumes)

Every report opens with the assumptions the engine had to make. Know them before you
trust a number:

- **No shift calendar yet** — pools are treated as always on shift. If your floor runs one
  shift, the twin will look faster than reality until you account for it.
- **Disruptions are opt-in** — a model with no `breakdown`/`absence` assumes everything
  goes right. A schedule that assumes nothing breaks is exactly the one a plant manager
  distrusts. Add disruptions (see
  [modeling.md](modeling.md#disruptions-seeded-reproducible)) before you promise dates.
- **Defaults are least-restrictive** — where data is missing, the twin picks the option
  that constrains the floor least, and names it as an assumption. Replace assumptions with
  your real data as you get it.
- **Instantaneous vs lead-time refills** — check your stock `lead_time`; a default of 0
  means refills are instant, which flatters inventory.

Calibration is how you close the gap between these defaults and your real floor. Do it once
on real history, and re-do it when the floor changes.

## Next

- Put the calibrated twin to work choosing settings: [optimizing.md](optimizing.md).
- The full config surface you are tuning: [modeling.md](modeling.md).
- Plan-vs-actual and the connectors that feed real logs in: [adapters.md](adapters.md).
