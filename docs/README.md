# twinflow documentation

![A factory floor and its glowing digital twin](images/twin-concept.png)

The complete manual. Whether you have never opened twinflow or you are looking up one
config field, start here.

Product direction: [Enterprise roadmap](enterprise-roadmap.md) and its linked
[implementation playbook](enterprise-build-playbook.md) describe the proposed path to
agent-driven manufacturing, front-office, and complex supply-chain decision twins.

For the implemented enterprise branch, start with the [operator workspace guide](enterprise/README.md), [agent integration guide](enterprise/AGENT-GUIDE.md), and [build status](enterprise/BUILD-STATUS.md).

## New here? Follow this path

1. [concepts.md](concepts.md) — what a digital twin is here, the vocabulary, and why
   every answer comes with a confidence range. **Read this first.**
2. [install.md](install.md) — install the engine, the API, and the front end.
3. [quickstart.md](quickstart.md) — from nothing to a running twin in five minutes.
4. [modeling.md](modeling.md) — describe your own floor in `model.yaml`.
5. [running.md](running.md) — run it and read the results.
6. [optimizing.md](optimizing.md) — stop reporting, start deciding.

## How-to guides

Step-by-step guides in plain words, for planners who are not programmers. Start with the [how-to guides index](how-to/README.md).

1. [Build a twin](how-to/build-a-twin.md): start a new folder from a starter template.
2. [Load your data and economics](how-to/load-data-and-economics.md): enter products, demand, prices and costs.
3. [Check your twin](how-to/check-your-twin.md): find mistakes before you run.
4. [Run your twin](how-to/run-your-twin.md): run a plant and turn on state capture for a job shop.
5. [Test orders](how-to/test-orders.md): run a shop's orders and read the due-date results.
6. [Write a policy](how-to/write-a-policy.md): write your own rule for what to make next.
7. [Write a reward, view, feature or demand](how-to/write-a-reward-view-feature-or-demand.md): change how days are scored, labeled, or drawn.
8. [Race your policies](how-to/race-your-policies.md): test many rules on the same random demand.
9. [Read the Markov map](how-to/read-the-markov-map.md): see how the plant moves between states.
10. [Improve a policy](how-to/improve-a-policy.md): test a new rule against your best one.
11. [Tune everything](how-to/tune-everything.md): learn where the default numbers are set.

## Full reference (look anything up)

**Model & data**
| Doc | Covers |
|---|---|
| [concepts.md](concepts.md) | Mental model, vocabulary, uncertainty, reproducibility |
| [modeling.md](modeling.md) | The complete `model.yaml` reference — every section and field |
| [data.md](data.md) | Every data contract in and out: plan, event log, inventory, KPI/JSON artifacts |
| [supply-chain.md](supply-chain.md) | Stocks, reorder points, lead time, multi-echelon, inventory KPIs |
| [active-control.md](active-control.md) | Dispatch rules, order release, disruptions (breakdown/absence/rush) |
| [glossary.md](glossary.md) | Every term, defined |
| [enterprise/rm-06/SPEC.md](enterprise/rm-06/SPEC.md) | Scheduling problem model v2: model, overrides, objectives, solvers, verifier, contracts |
| [../examples/README.md](../examples/README.md) | Importable floor examples, parameter sweeps, and scenario capsules |

**Using it**
| Doc | Covers |
|---|---|
| [quickstart.md](quickstart.md) | End to end in five minutes |
| [running.md](running.md) | Running the twin, replications, reading the report and every KPI |
| [optimizing.md](optimizing.md) | Sweeps, objectives, costs, optimizers, ML surrogates, inventory optimization |
| [tuning.md](tuning.md) | Variation, calibration against real history, sensitivity, guardrails |
| [cli.md](cli.md) | Every `twinflow` command and flag |
| [frontend.md](frontend.md) | The web UI: every tab and what you do in it |
| [troubleshooting.md](troubleshooting.md) | Problems, causes, fixes |

**Extending & integrating**
| Doc | Covers |
|---|---|
| [modules.md](modules.md) | The module surface: objectives, costs, optimizers, models — and how to add your own |
| [adapters.md](adapters.md) | Integration surfaces: ERP/MES connectors, demand, forecasting, plan-vs-actual |
| [api.md](api.md) | The local REST API — endpoints and job polling |
| [architecture.md](architecture.md) | The five-layer design, for developers |

## The shape of the system

![The shape of the system](images/diagram-system.png)

The one rule the whole system rests on: **a new client is data, never code.** You never
write a simulation by hand — you declare what the floor does, and the engine builds the
mechanism.

## Three ways to drive it

- **The `twinflow` command** — legacy simulation commands (`validate`, `run`, `balance`,
  `report`, `optimize`, `serve`) plus workspace commands for scenarios, data,
  scheduling, and administration. See [CLI reference](cli.md), [enterprise workspace
  guide](enterprise/README.md), and [agent guide](enterprise/AGENT-GUIDE.md).
- **The Python library** — the same operations, callable in your own code. See
  [running.md](running.md) and [optimizing.md](optimizing.md).
- **The web UI** — map the floor and run / sweep / optimize in the browser. See
  [frontend.md](frontend.md).
