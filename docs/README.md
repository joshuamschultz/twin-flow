# twinflow documentation

Operating instructions for every feature and build. Start here.

| Guide | What it covers |
|---|---|
| [install.md](install.md) | Installing the engine, the API extra, and the front end |
| [cli.md](cli.md) | Every `twinflow` command: `validate`, `run`, `balance`, `report`, `optimize`, `serve` |
| [modeling.md](modeling.md) | The full `model.yaml` vocabulary, including the Tier 0 capabilities |
| [modules.md](modules.md) | The module surface: objectives, cost functions, optimizers, ML models — and how to add your own |
| [adapters.md](adapters.md) | The integration surfaces: ERP/MES connectors, demand generation, forecasting, dispatch policies, RL environment, plan-vs-actual — where real systems plug in |
| [api.md](api.md) | The local REST API (endpoints, request/response shapes, job polling) |
| [frontend.md](frontend.md) | The React front end: install, run, build |
| [quickstart.md](quickstart.md) | End to end in five minutes, from install to a running twin in the browser |

## The shape of the system

```
model.yaml + plan.csv              a complete client, pure config
        │
   twinflow.model  ── compile ──►  CompiledModel (routing graph + BOM)
        │
   twinflow.plan   ── run ──────►  event log ──► KPIs + confidence bands
        │
   twinflow.modules               ScoringSurface: scenario in, KPIs out
        │                           ├─ objectives   (KPIs → one score)
        │                           ├─ cost functions (config + KPIs → money)
        │                           ├─ optimizers   (search the lever space)
        │                           └─ surrogate models (learn the surface)
        │
   twinflow.service (REST) ──────► web/ (React front end)
```

Everything above `twinflow.model` is driven by the same rule: a new client is data,
never code.
