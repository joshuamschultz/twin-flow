# Twinflow enterprise implementation playbook

Companion to the [enterprise roadmap](enterprise-roadmap.md), reviewed 2026-09-12 against repository `af5500d`. Sections map one-to-one to roadmap steps. The roadmap owns priority, business outcomes, dependencies, and release gates; this file describes how to build them.

All new paths, APIs, schemas, and snippets below are **proposed designs**. They do not exist merely because they appear here. Snippets illustrate a narrow implementation seam; they are not complete production implementations. Existing extension points are linked separately. Tools were checked against their official repositories/documentation; select and lock versions in each implementation PR rather than installing moving branches. Recommendations are engineering judgments, not comparative benchmarks.

## Architecture to preserve and extend

Keep SimPy, Python, the compiler boundary, Polars/Parquet, the scoring concept, FastAPI, and React. Separate three forms of state:

| Object | Meaning | Identity and persistence |
|---|---|---|
| Model version | Processes, resources, constraints, distributions, domain profile | Immutable revision plus canonical content digest |
| Operational snapshot | Known orders, WIP, stock, documents, resources and observed events as of a time | Immutable snapshot ID, source watermarks, event-time and ingestion-time cutoffs |
| Scenario / experiment | Base model+snapshot, proposed changes, objectives, policy, budget, seeds | Immutable scenario version; separately tracked execution attempts and results |

Avoid colliding with today's `modules.surface.Scenario`, which means only lever overrides. Initially call the new aggregate `ScenarioCapsule` and keep `Scenario` as a compatibility adapter; introduce a separately versioned public SDK before renaming old types.

Suggested package additions, introduced incrementally:

```text
src/twinflow/
  scenario/       # versioned capsule schemas, migration, validation, semantic edits
  domain/         # shared entity/state contracts; manufacturing, office, network packs
  application/    # authenticated use cases shared by REST, SDK, and MCP
  experiments/    # run specification, result contracts, comparisons, evidence
  persistence/    # repository interfaces plus database/artifact implementations
  integrations/   # external schemas -> domain facts; controlled read/write boundaries
  mcp/           # thin transport adapter
  workers/       # durable execution activities and isolated simulation processes
```

Preserve existing layer boundaries: engine/primitives do not import service, persistence, connectors, or an agent framework. Put stable shared contracts below their consumers. Extend [boundary tests](../tests/test_boundaries.py) deliberately as packages are added. Domain packs compile to shared execution primitives plus explicit domain events; do not route every concept through a physical material transform.

Use local filesystem artifacts for offline mode. Use PostgreSQL for service metadata and an object store for retained inputs/results when introducing enterprise hosting. An event table and immutable snapshots are sufficient initially; a separate graph database, streaming platform, or distributed simulator needs a measured justification.

<a id="rm-01"></a>
## RM-01 — Correctness, statistical meaning, and execution isolation

[Roadmap outcome and gate](enterprise-roadmap.md#rm-01).

**Modify first:** [KPIs](../src/twinflow/instrumentation/kpis.py), [aggregation](../src/twinflow/instrumentation/aggregate.py), [scoring surface](../src/twinflow/modules/surface.py), [objectives](../src/twinflow/modules/objectives.py), [run driver](../src/twinflow/plan/driver.py), [replication](../src/twinflow/plan/replication.py), [reconciliation](../src/twinflow/adapters/reconcile.py), [run stamps](../src/twinflow/run_stamp.py).

**Build sequence:**

1. Create independent tiny fixtures for: an order stopped after its first operation; all-scrap output; a material-blocked job; multiple lots satisfying one order; two parallel machines with different setups; and two concurrently submitted optimizations. Use these to establish failures before changing behavior.
2. Add an order fulfillment ledger driven by accepted terminal outputs. Track required, accepted, scrapped, shipped, and remaining quantities; partial shipments and completion are distinct. Record `completed`, `incomplete_at_horizon`, `infeasible`, and `failed` outcomes explicitly. Count known demand in the on-time denominator according to the declared observation horizon; distinguish not-yet-due from late and unknown.
3. Add both simulated-time and wall-clock/event-count budgets. Natural event exhaustion does not prove all orders finished. Return unresolved waits and remaining demand. End bounded rework/disruption runs without inventing completions.
4. Replace the ambiguous `Interval` contract with separate outcome quantiles, confidence interval on an estimated mean, on-time probability and its interval, replication count, and completion/censoring counts. If P90 lies beyond the simulated horizon, return a lower bound or `not_estimable`, not the P90 of the completed subset. Keep input/model uncertainty distinct from sampling noise.
5. Store per-replication order, resource, inventory, and cost metrics in `Evaluation`. Aggregate every objective over its declared distribution/statistic. Retain the representative trace only for visualization. Define fill rate, on-time-in-full, and stock availability as separate metrics; today's `service_level` is a stock-empty-time proxy.
6. Wire the existing paired-difference calculation into API/report output after testing pairing. Fixed per-source streams do not alone guarantee the same job gets the same draw after dispatch changes. Prefer stable keyed randomness for comparable events: `(seed, replication, source, entity_id, operation_id, occurrence)` hashed with a documented algorithm. Treat entity/topology changes separately and preserve valid marginal distributions.
7. Eliminate process-wide `os.chdir`. Pass `artifact_dir`, run ID, and execution settings explicitly through scoring, replication, and driver. Bound total compute across requests; do not give every request its own unrestricted CPU-sized pool. Preserve absolute paths only inside trusted worker code.
8. Retain model, plan/snapshot, resolved defaults, plugin versions, seed scheme, engine commit, dependency lock/container digest, and result schema. Canonical numeric/result fields are reproducibility targets; timestamps/run IDs are intentionally different.

**Statistical kernel sketch** — complete paired samples, at least two independent replications; this is a CI on a mean difference, not a delivery-date interval:

```python
import numpy as np
from scipy.stats import t

def paired_mean_ci(baseline, candidate, confidence=0.95):
    a, b = np.asarray(baseline, dtype=float), np.asarray(candidate, dtype=float)
    if a.ndim != 1 or a.shape != b.shape or a.size < 2:
        raise ValueError("Need at least two aligned replication pairs")
    if not (0 < confidence < 1 and np.isfinite(a).all() and np.isfinite(b).all()):
        raise ValueError("Invalid level or missing/nonfinite observations")
    delta = b - a
    half = t.ppf((1 + confidence) / 2, delta.size - 1) * delta.std(ddof=1) / np.sqrt(delta.size)
    return {"mean_delta": float(delta.mean()),
            "mean_ci": [float(delta.mean() - half), float(delta.mean() + half)]}
```

**Tools:** retain NumPy/SciPy/pytest; add [Hypothesis](https://github.com/HypothesisWorks/hypothesis) for generated conservation, ordering, and isolation tests. Property tests complement manually calculated cases; they do not establish validity against real operations.

**Acceptance:** no order disappears from accounting; no arbitrary replacement of missing values with zero; utilization uses recorded resource identity and available resource-time; blocked/material/labor waits have evidence; paired statistics match independent calculations; canceled and concurrent runs preserve isolation. Update public metric descriptions and the misleading calibration/variability wording in existing docs.

<a id="rm-02"></a>
## RM-02 — Single-file scenario contracts and import/export

[Roadmap outcome and gate](enterprise-roadmap.md#rm-02).

**Architecture:** create a typed external capsule schema that compiles into existing model/plan contracts. Keep parsing, semantic validation, and compilation separate. Adapt legacy files into the same internal representation instead of maintaining two engines.

**Tools:** [Pydantic](https://github.com/pydantic/pydantic) for typed boundaries and schema generation; [python-jsonschema](https://github.com/python-jsonschema/jsonschema) for published-schema conformance tests. Keep the safe YAML entry point in [model loader](../src/twinflow/model/loader.py); refactor it into one reusable parsing boundary if necessary.

**Build sequence:**

1. Specify `schema_version`, `model`, `snapshot`, `experiment`, `assumptions`, `provenance`, and `required_capabilities`. Use stable entity IDs; names are labels. Distinguish absent, unknown, zero, unlimited, and estimated values.
2. Declare units and normalize at compile time. Handle dimensional conversion explicitly; never treat mass and pieces as interchangeable without a recipe. Include timezone-aware origin/as-of time, versioned calendars, currencies with dated conversion assumptions where needed, and document/part revisions.
3. Define primitive/domain schemas plus referential and semantic validation: duplicate IDs, BOM cycles, illegal route targets, unbounded rework, incompatible units, overallocated stock, missing qualifications, and WIP inconsistent with its route revision. Reject unknown required fields and unsupported mandatory capabilities.
4. Add a deterministic, non-destructive migration chain. Preserve the original capsule and record migration decisions. Export a canonical resolved representation and digest; formatting changes should not alter semantic identity. Specify canonical numeric/Unicode/ordering rules; exclude the digest field itself from hashing.
5. Build CLI import/validate/run/export and a browser drop-file → issues → assumptions → baseline flow. Validation errors include entity, field path, severity, and suggested repair. Agent and UI edits call the same patch service.
6. Define package limits before uploads: byte count, entity count, YAML nesting/aliases, expression complexity, and total scenario evaluation budget. An optional `.twin` ZIP archive must reject traversal paths, symlinks, decompression bombs, and unlisted contents; verify manifest digests. Connector credentials never enter the capsule.

**Proposed capsule excerpt** — abbreviated design, not a runnable current example:

```yaml
schema_version: "0.1"
required_capabilities: [manufacturing.calendar, manufacturing.qualified_resources]
model:
  id: precision-shop
  revision: "4"
  profile: hmlv
  time: {unit: second, timezone: America/Chicago}
  # Typed resources, calendars, processes, routes and material definitions here.
snapshot:
  id: snapshot-104
  as_of: "2026-09-12T08:00:00-05:00"
  model_revision: "4"
  # Explicit demand, stock, WIP, outstanding POs and resource state here.
experiment:
  question: capacity_to_promise
  seed: 42
  replications: 100
  horizon_seconds: 2592000
  max_wall_seconds: 120
  baseline_snapshot: snapshot-104
assumptions:
  - id: A-001
    field: model.processes.finish.duration
    status: estimated
    basis: operator_interview
provenance:
  source: customer_export
  captured_at: "2026-09-12T08:00:00-05:00"
```

**Proposed API usage** — implement these commands in this step:

```bash
twinflow scenario import --model model.yaml --plan plan.csv --out shop.twin.yaml
twinflow scenario validate shop.twin.yaml
twinflow scenario run shop.twin.yaml --out ./artifacts/baseline
```

**Acceptance:** migration fixtures for every existing example; canonical round-trip equality; API/CLI identical diagnostics; malicious/oversized input rejection; documented small-model loading target; fully offline baseline from a complete capsule. A draft schema excerpt must never be presented as a complete runnable example in the final product documentation.

<a id="rm-03"></a>
## RM-03 — HMLV mechanics and credible dates

[Roadmap outcome and gate](enterprise-roadmap.md#rm-03).

**Architecture:** replace anonymous capacity slots with resource instances, while retaining capacity shorthand as config sugar that expands into instances. Operations request a feasible combination of machine, labor, tooling, material, and prerequisite information. The scheduler proposes assignments; the simulator enforces them. Build on [location](../src/twinflow/primitives/location.py), [labor](../src/twinflow/primitives/labor.py), [machine](../src/twinflow/primitives/cell.py), and [driver](../src/twinflow/plan/driver.py).

**Build sequence:**

1. Implement calendars with exceptions, breaks, holidays, overtime, timezone/DST handling, and explicit preemption behavior. Store timeline instants consistently; convert recurring local working periods into simulation intervals. An operation crossing shift end must know whether it pauses, finishes unattended, or requires an authorized extension.
2. Add machine identity and per-machine setup, maintenance, eligibility and speed. Record actual operator assignments and separate labor attendance from machine occupancy. Charge costs from the correct time ledger.
3. Model required capabilities and dated qualifications, alternatives, fixtures, shared tools, and simultaneous resource needs. Define atomic reservation and release semantics to avoid holding a scarce resource forever while waiting for another. Preserve or deliberately revise [acquisition rules](../src/twinflow/engine/acquire.py).
4. Add operation-level material requirements, multi-input assembly readiness, allocation/pegging, partial kits where allowed, and qualified substitutions. Ensure an alternate resource cannot silently change route requirements.
5. Make WIP state explicit: completed operations, remaining processing/hold time, reserved resources/material, accepted/rejected quantities, route revision, and outstanding inspections. Track split/merge genealogy independently from lot naming conventions.
6. Add bounded rework loops, route alternatives, transfer batches, outside processing, and finite downstream buffers with explicit blocking behavior. Model setup versus process/transfer batch sizes separately.
7. Publish order forecast objects with completion/shipment distinction, customer-local dates, quantiles, feasibility, upstream dependencies, and existing-order impact.

**Minimal state sketch:**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ResourceRequirement:
    capability: str
    count: int
    qualification_rule: str | None

@dataclass(frozen=True)
class OperationState:
    operation_id: str
    order_id: str
    route_revision: str
    remaining_work_seconds: float
    required_resources: tuple[ResourceRequirement, ...]
    prerequisite_ids: tuple[str, ...]
    # State references identify material, information and precedence gates.
```

**Tools:** existing SimPy for event execution; standard-library `zoneinfo` for timezone conversion; versioned customer calendar data. Prototype calendar and constraint cases directly before adding a scheduling dependency in RM-06. These choices keep the physics independently testable.

**Acceptance:** hand-solved cases for overnight shifts, DST boundaries, different setups on two machines, attended/unattended time, expiring skills, incompatible tooling, split lots feeding assembly, and WIP on a superseded route. Ensure a capacity increase is not assumed to improve every objective under every dispatch policy; test physical invariants rather than false monotonicity rules.

<a id="rm-04"></a>
## RM-04 — Agent contracts and a shared application layer

[Roadmap outcome and gate](enterprise-roadmap.md#rm-04).

**Architecture:** domain-aware application functions own authorization, version checks, validation, budgeting, and evidence. REST and MCP are adapters. The local Python SDK may use an in-process backend; remote clients use the same contracts through HTTP. Capability discovery reports implemented semantics and model-specific supported actions.

The [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) supplies the transport implementation. Pin its chosen major version: the repository reviewed here documents the v2 API, so old v1 `FastMCP` snippets should not be copied without checking compatibility. For remote clients, implement the relevant [MCP authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization) with issuer/audience validation and scoped access. Protocol authorization does not replace Twinflow's object-level access checks.

**Tool contract:**

| Tool | Inputs | Return |
|---|---|---|
| `describe_capabilities` | Scenario/model reference | Supported decisions, assumptions, schemas, allowed levers and units |
| `import_scenario` / `validate_scenario` | Uploaded content reference / immutable scenario version | Validation issues and provisional/valid status |
| `branch_scenario` / `patch_scenario` | Parent version, semantic edits, expected revision | New immutable version, diff, validation result |
| `evaluate` | Scenario, snapshot, experiment settings, idempotency key | Job ID and estimated budget consumption |
| `compare` / `optimize` | Baseline, candidates or allowed search domain | Job ID; later evidence-backed alternatives |
| `get_job` / `cancel_job` | Scoped job ID | Durable state, progress, cost and cancellation status |
| `get_forecast` / `explain_result` | Result plus entity IDs | Structured forecast, model trace and limitations |
| `query_evidence` | Result ID and bounded filters | Authorized, paginated event/constraint evidence |
| `propose_action` | Specific evaluated scenario/result and intended change | Draft proposal; no operational mutation |

Prefer semantic edits such as `set_resource_calendar` or `change_order_release` to unrestricted dictionary paths. Validate editable fields, type, units, range, and model capability. A scenario cannot authorize itself, change tenant ownership, or grant operational write access.

**Adapter sketch** — the application/auth functions are proposed, not existing imports:

```python
from mcp.server import MCPServer

mcp = MCPServer("Twinflow")

@mcp.tool()
async def evaluate(scenario_version: str, request_key: str) -> dict:
    """Submit a bounded simulation; returns a job reference, not a forecast."""
    principal = principal_from_verified_transport()  # server-controlled context
    request = build_evaluation_request(scenario_version, request_key)
    job = await application.submit_evaluation(principal, request)
    return {"job_id": job.id, "status": job.status, "result_ready": False}
```

**Build sequence:** extract use cases from [service app](../src/twinflow/service/app.py); type input AND output schemas; add scenario registry/version checks; implement budget reservation and idempotency; add REST; wrap in SDK/MCP; add evidence queries; then add task benchmark fixtures. Idempotency is scoped to principal/project and operation; reuse with a different request digest returns conflict.

Normal result envelopes include `schema_version`, scenario/snapshot/result IDs, `as_of`, freshness, model validity status, outcome metrics, assumptions, infeasibility reasons, replication information, evidence references, and compute usage. Do not send unrestricted Parquet logs to a model context. Treat all scenario notes and imported documents as untrusted content, never policy instructions.

**Acceptance:** two clients complete import → validate → branch → evaluate → compare → explain; stale versions and forbidden edits fail with repairable errors; canceled jobs stop consuming budget within a published bound; one user cannot retrieve another project's job/artifact. Add a set of intentionally unsupported questions to ensure the agent can abstain.

<a id="rm-05"></a>
## RM-05 — Operational data, snapshots, and validation

[Roadmap outcome and gate](enterprise-roadmap.md#rm-05).

**Architecture:** connector → raw immutable records → normalization/quality checks → entity/event store → reconciled snapshot → experiment. Keep raw facts, corrections, inferred state, and simulated events distinguishable. Actual facts do not need to mimic the simulator's existing event table at ingestion.

Extend [connector protocols](../src/twinflow/adapters/connectors.py) with capability contracts for orders, operation completions, WIP, inventory transactions, purchasing, calendars, and revisions. Choose one partner's real source system and its supported read interface. CSV export can be the first transport; the quality of identity, mapping, and reconciliation matters more than a connector logo.

**Tools:** PostgreSQL for transactional metadata/events; existing Polars/Parquet for bulk processing; optional [DuckDB](https://github.com/duckdb/duckdb) for portable analytical queries over customer history. Begin with scheduled imports; adopt change-data capture only when source permissions and freshness requirements justify it.

**Normalized event shape:**

```json
{
  "event_id": "erp-event-84721",
  "source": "customer-erp",
  "source_revision": "3",
  "occurred_at": "2026-09-11T16:24:00Z",
  "ingested_at": "2026-09-11T16:25:10Z",
  "entity_type": "operation",
  "entity_id": "op-235",
  "event_type": "accepted_quantity_recorded",
  "payload": {"quantity": 8, "unit": "piece", "lot_id": "lot-92"},
  "supersedes_event_id": null
}
```

Tenant/source authorization comes from the connector context, not a trusted field in uploaded JSON. Decide whether source IDs identify immutable events or mutable records; deduplicate accordingly, keeping source revisions and explicit corrections.

**Build sequence:**

1. Agree source-of-truth ownership by field and map stable IDs across ERP/MES/PLM/office systems. Quarantine unknown units, duplicate revisions, impossible event order, and unresolvable references. Record watermark, mapping version, counts, and errors.
2. Build snapshots reproducibly from a known data cutoff. Record both occurred-at and known-at semantics so a late correction cannot leak into a historical backtest. Reconcile stock balances, WIP progress, purchase receipts, and open demand.
3. Define freshness requirements per decision/input. Yesterday's engineering routing may be acceptable; yesterday's bottleneck breakdown state may not be. Return a readiness assessment naming stale or missing dependencies.
4. Estimate distributions from active durations, setup/changeovers, interruptions, and quality outcomes, rather than fitting all elapsed time as processing. For sparse HMLV data, share estimates across justified part/process families and expose uncertainty in that sharing. Reserve expert estimates as explicit provisional assumptions.
5. Run chronological rolling-origin backtests against immutable historical snapshots. Separate fitting, model selection, and final evaluation periods. Score date error, quantile loss, interval coverage/width, probability calibration, and service outcomes against planner/ERP and simple heuristic baselines.
6. Register approved model versions by decision and operating range. Monitor mix, lead-time, bottleneck, and calibration drift; trigger review/revalidation. Recalibration produces a candidate version, never silent replacement of a published model.

**Backtest orchestration sketch:**

```python
for cutoff in historical_decision_times:
    snapshot = snapshots.as_known_at(cutoff)  # exclude subsequently learned records
    model = registry.approved_as_of(cutoff, decision="promise_date")
    forecast = experiments.evaluate(model, snapshot, fixed_policy)
    score_store.append(score_when_outcomes_mature(forecast, actuals))
```

This is an evaluation workflow design. Implement maturity/censoring and cohort definitions explicitly. Observations from the same order, week, or supplier can be dependent; use appropriate grouped uncertainty estimates rather than treating every event row as independent evidence.

**Acceptance:** duplicated, reordered, delayed, and corrected source events yield the intended snapshot; raw-to-normalized lineage is queryable; holding out the future changes fitted estimates; incomplete/unknown cases remain explicit; a partner accepts the forecast validation report. “Calibration matched historical average KPIs” is insufficient evidence of forecast validity.

<a id="rm-06"></a>
## RM-06 — Solver-assisted scheduling and robust optimization

[Roadmap outcome and gate](enterprise-roadmap.md#rm-06).

**Architecture:** compile validated constraints into a deterministic candidate scheduler; independently validate its schedule; simulate execution under uncertainty; retain a Pareto set and evidence. Preserve the existing [optimizer registry](../src/twinflow/modules/optimizers.py) as an integration point, but add typed hard constraints and schedule artifacts rather than encoding everything as scalar lever values.

**Tools:** [OR-Tools](https://github.com/google/or-tools), whose [job-shop guide](https://developers.google.com/optimization/scheduling/job_shop) demonstrates precedence and machine non-overlap; [Optuna](https://github.com/optuna/optuna) for bounded search over parameterized policies; [SALib](https://github.com/SALib/SALib) for sensitivity methods. Adopt the scheduler first; add search/sensitivity libraries when a measured question warrants them. Maintain a release-specific license/dependency inventory for every adopted solver and plugin.

**Build sequence:**

1. Separate hard feasibility (qualification, material availability, calendar, precedence, frozen commitments) from preferences (tardiness, overtime, setup reduction, schedule stability). A large penalty is not a substitute for a hard constraint.
2. Compile optional intervals for eligible alternate machines, precedence for operations, no-overlap/cumulative resource constraints, and calendar exclusions. Include material and information readiness. Choose and document integer time resolution and rounding so the solver cannot exploit rounding to create false feasibility.
3. Begin with dispatch heuristics as feasible baselines and solver hints. Set time limits; preserve feasible incumbents; expose `optimal`, `feasible`, `infeasible`, or `unknown` truthfully. Report objective bound/gap only when available.
4. Replay proposed schedules in the simulator using fixed versus flexible dispatch semantics explicitly. The simulator must not silently discard the schedule and optimize a different policy.
5. Rank candidates across replications using business-defined metrics. Separate search seeds from independent validation seeds; test winner selection bias and correlated disruptions. Use paired comparisons only when scenario/event pairing is meaningful.
6. Produce alternatives with incremental cost, risk, service impact, and schedule churn. Find binding constraints with targeted relax-and-rerun experiments. Call these effects “within this model”; they are not automatically causal effects in the real operation.

**Small OR-Tools feasibility example** — two sequential operations sharing one machine; later add optional resources, calendars, and other constraints:

```python
from ortools.sat.python import cp_model

model = cp_model.CpModel()
starts = [model.new_int_var(0, 100, f"start_{i}") for i in range(2)]
ends = [model.new_int_var(0, 100, f"end_{i}") for i in range(2)]
tasks = [model.new_interval_var(starts[i], d, ends[i], f"task_{i}")
         for i, d in enumerate([12, 18])]
model.add_no_overlap(tasks)
model.add(starts[1] >= ends[0])
model.minimize(ends[1])
solver = cp_model.CpSolver()
solver.parameters.max_time_in_seconds = 5
status = solver.solve(model)
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    schedule = [(solver.value(s), solver.value(e)) for s, e in zip(starts, ends)]
else:
    schedule = None  # UNKNOWN does not prove infeasibility.
```

**Acceptance:** independently check assignments and all constraints; compare small cases with enumeration/known optima; benchmark against FIFO/EDD/SPT and current planning practice; retain results on fresh seeds; report no date when no validated feasible plan exists. Do not maximize utilization as a default goal: high utilization can worsen queueing and responsiveness.

<a id="rm-07"></a>
## RM-07 — Front-office cases and information dependencies

[Roadmap outcome and gate](enterprise-roadmap.md#rm-07).

**Architecture:** introduce a case/workflow domain pack. Shared primitives handle queuing and resource contention; case state handles document availability, revision, decisions, identities, and audit history. A document can satisfy multiple consumers; reading it does not decrement material stock. An approval is an evidence-bearing state transition.

**Build sequence:**

1. Define `Case`, `Task`, `InformationArtifact`, `Decision`, and `Dependency`. Support many-to-many case/order/document links rather than forcing every event into one work-order ID.
2. Support sequential tasks, parallel fork/join, exclusive decisions, bounded correction loops, delegation/escalation, and cancellation. Specify join identity and document revision matching to avoid mixing branches from different versions.
3. Implement business-time SLAs, worker skills/roles, external response times, workload caps, and separation of requester/approver where configured. Distinguish active work from queues and unanswered questions.
4. Build a quote → engineering review → purchasing/quality review → release example that gates shop work. A changed drawing invalidates dependent approval only according to the configured rule; preserve prior evidence.
5. Import audit/case events and fit durations/branch rates. Let process mining suggest draft flows, with human review of exceptional paths, missing events, and policy constraints before simulation.
6. Extend the UI's floor graph into a process graph with domain labels, age/queue overlays, prerequisite state, and evidence. Use the same agent discovery and experiment APIs.

**Proposed rule shape:**

```yaml
tasks:
  - id: release_to_production
    kind: approval
    requires:
      all:
        - {artifact: drawing, revision: order.required_drawing_revision, state: approved}
        - {task: quality_review, state: completed}
        - {task: purchase_review, state: completed}
    assigned_role: release_authority
    separation_of_duties: {different_from: case.requester}
    on_reject: engineering_correction
    max_rework_cycles: 3
```

This DSL is proposed. Resolve references with typed operators and a restricted interpreter; values such as `order.required_drawing_revision` are declarative references, not Python code. Evaluate rules at the required transition time and version their results.

**Tools:** retain SimPy for contention and duration behavior. Consider [PM4Py](https://github.com/process-intelligence-solutions/pm4py) for optional process discovery/conformance analysis. Its published [license](https://github.com/process-intelligence-solutions/pm4py/blob/release/LICENSE) is AGPL; assess the exact intended integration and licensing arrangement before adopting it in this proprietary product. A custom event-to-draft-flow importer is a viable initial scope. Do not assume every BPMN diagram has executable Twinflow semantics; support a documented subset if BPMN interchange is later requested.

**Acceptance:** fork/join and revision-correction cases have exact expected transitions; wrong-role or stale-revision approval cannot release work; office and plant clocks interact correctly; document sharing preserves material accounting; office forecasts pass their own validation cohort. Measure case turnaround, age, rework, handoffs, and downstream ship-date impact.

Also ship a standalone office capsule, such as invoice resolution or purchase approval, that has no manufacturing entities. Its baseline and agent workflow must run through the same import and evaluation contracts. Likewise, RM-08 must include a distribution-only network fixture so supply-chain modeling does not require a factory.

<a id="rm-08"></a>
## RM-08 — Industry-neutral complex supply networks

[Roadmap outcome and gate](enterprise-roadmap.md#rm-08).

**Architecture:** model a typed network of sites, suppliers, processes, material items, parts, assemblies, orders, shipments, documents, and qualification rules. Supplier → supplier is only one relationship; BOM dependencies, process prerequisites, allocations, and document gates are different edges. Use relational tables and indexed edges first; introduce a graph database only if measured traversal requirements warrant it.

The kernel remains industry-neutral. Aerospace is one test profile for complexity. Other profiles can add shelf life/cold-chain evidence, component substitutions, customer source approvals, test certificates, or product-specific release rules. Each rule has an owner, version, scope, effective dates, and required evidence. A generic “compliant: true” flag is inadequate.

**Build sequence:**

1. Model item/revision/effectivity, engineering versus manufacturing BOM distinctions, assembly multiplicity, units, alternates, yield, and route dependencies. Retain genealogy through splitting, combining, rework, consumption, and shipment.
2. Add explicit purchase lines, partial receipts, supplier capacity calendars, shipments in transit, allocation/reservation, minimum order quantities, lot multiples, and economic cost components. Prevent the same stock or expected receipt satisfying two incompatible promises.
3. Add process/site qualification, quality inspections, quarantine, release documents, expiry, substitutions, and customer-specific restrictions. Evaluate rule applicability by part, process, supplier, customer, jurisdiction/profile, and date as configured and approved by responsible users.
4. Compose each site's internal process with transport lanes, cutoffs, external processing, and shared supplier resources. Define capacity time buckets or detailed operations consistently so coarse supplier models do not overpromise capacity consumed by detailed site models.
5. Add correlated disruption scenarios: one sub-tier outage affects every dependent path; one document hold can delay a whole lot; one transport disruption affects multiple shipments. Parameterize uncertainty and preserve unknown capacity rather than assuming infinity.
6. Implement capability-to-promise across available stock, buildable assemblies, expected receipts, qualified processes, and documentary release. Return critical dependencies and controlled mitigation alternatives: expedite, reallocate, reroute, split delivery, or use an approved substitute.
7. Add partner-level data visibility and aggregate capacity sharing. A buyer may see an availability commitment without receiving the supplier's proprietary routing or other customers' orders.

**Proposed gate evaluation result:**

```json
{
  "gate_id": "gate-783",
  "subject": {"lot_id": "lot-92", "process_id": "surface-treatment"},
  "rule_revision": "customer-release-12",
  "evaluated_at": "2026-09-12T13:00:00Z",
  "status": "blocked",
  "reasons": ["missing_test_certificate", "supplier_process_approval_expired"],
  "evidence_refs": ["record-qualification-392"],
  "affected_order_ids": ["order-17", "order-21"]
}
```

The gate engine checks evidence against configured rules. It does not interpret legislation or issue a legal certification. Domain owners maintain applicable rules; Twinflow records which version was evaluated and which evidence supported the result.

**Tools:** existing Polars for BOM/exposure rollups; PostgreSQL for revision and allocation transactions; OR-Tools from RM-06 for constrained allocation/scheduling. Reuse the rule interpreter from RM-07 and the event/snapshot pipeline from RM-05. Avoid procuring a separate “supply-chain AI” component until a specific uncovered capability is demonstrated.

**Acceptance:** test a network with at least three supplier tiers, alternative processes, shared sub-tier capacity, components feeding assembly, split shipments, quality holds, document expiry, and engineering changes. Add at least two contrasting industry profiles to prove that core code has no aerospace-only branching. Confirm quantity conservation, no double allocation, correct revision effectivity, traceable affected dates, and access separation between partners. Validate against actual network history where available; label unobserved supplier mechanisms as assumed.

<a id="rm-09"></a>
## RM-09 — Durable service, security, and enterprise operation

[Roadmap outcome and gate](enterprise-roadmap.md#rm-09).

**Architecture:** a modular API/application service, durable metadata store, artifact store, and separately constrained compute workers. One deployment per enterprise is a valid first model. Shared tenancy requires isolation in metadata, objects, caches, worker scratch space, queues, logs, backups, and exports.

**Tools:** [Temporal's Python SDK](https://github.com/temporalio/sdk-python) for durable orchestration; PostgreSQL metadata with [row-security policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html); an enterprise identity provider or [Keycloak](https://github.com/keycloak/keycloak); [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) for traces/metrics; [Syft](https://github.com/anchore/syft) for SBOM generation and [Cosign](https://github.com/sigstore/cosign) for release signatures. These are incremental selections, not a requirement to deploy all services in the first pilot.

**Build sequence:**

1. Replace [in-memory JobStore](../src/twinflow/service/jobs.py) with a repository-backed state machine: `queued → running → succeeded/failed/canceled`, plus attempt records, heartbeat, deadlines, artifact state, and reserved/actual compute. Add idempotency and bounded retries.
2. Use durable workflows to orchestrate simulation **activities**. Run CPU-heavy simulation in isolated worker processes, not in the workflow interpreter. Retried activities write to attempt-specific staging paths and atomically publish a manifest; readers only see complete results. Pin workflow/activity versions and retain old workers for in-flight compatible execution where required.
3. Establish identity and project membership at ingress. Define roles such as viewer, modeler, experimenter, approver, operator, and administrator, with service identities for agents. Recheck authorization at artifact retrieval and writeback. A user-provided tenant ID is never authority.
4. Store immutable metadata and artifact references. Apply encryption, customer key integration where contracted, scoped object access, retention, deletion, backup, and restore procedures. Avoid secrets or sensitive document contents in tracing payloads. Validate archive imports and isolate expression execution with compute limits.
5. Set global/per-customer budgets; prevent Cartesian sweep explosions before enqueue. Add admission control, fair queuing, cancellation, memory/time limits, and bounded result sizes. Never return raw tracebacks to remote clients.
6. Trace API request → scenario → job attempt → worker → result with shared IDs. Measure queue delay, run duration, error rate, computation volume, artifact bytes, and calibration freshness. Define on-call procedures and customer-visible degraded behavior.
7. Add database/schema migration rehearsal, artifact compatibility, release rollback, vulnerability/dependency review, signed artifacts, and support/EOL policy. Test restore and cross-tenant denial in CI and deployment drills. Maintain procurement documentation, architecture/data-flow diagrams, incident contacts, and a responsibility matrix.

**PostgreSQL isolation sketch** — assumes table `scenario_versions(tenant_id uuid, ...)` exists:

```sql
ALTER TABLE scenario_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE scenario_versions FORCE ROW LEVEL SECURITY;

CREATE POLICY scenario_tenant_policy ON scenario_versions
USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid);
```

Set tenant context transaction-locally from verified application identity, using a parameterized database call. Use a non-superuser application role without `BYPASSRLS`; PostgreSQL documents privileged-role bypass behavior. Restrict direct database access and reset pooled context. RLS supplements application authorization; repeat isolation checks for artifacts and caches. See the [PostgreSQL policy documentation](https://www.postgresql.org/docs/current/ddl-rowsecurity.html).

**Acceptance:** kill a worker mid-run and recover without duplicate published results; retry an API request without duplicate billed work; verify a canceled process is actually stopped; restore metadata plus artifacts; prove unauthorized cross-project reads fail; rehearse a rollback. Do not call a deployment compliant or certified solely because these controls exist—customer-specific assessments and evidence are a separate acceptance activity.

<a id="rm-10"></a>
## RM-10 — Scale, policy interaction, and controlled writeback

[Roadmap outcome and gate](enterprise-roadmap.md#rm-10).

**Architecture:** scale experiments across independent workers, keeping each simulation coherent. Add a stepwise policy interface at engine decision boundaries after the offline contracts stabilize. Keep operational side effects outside the simulation process and outside ordinary evaluation tools.

**Build sequence:**

1. Define benchmark tiers and record hardware, time horizon, entities, operations/events, replications, candidate count, fidelity, artifact volume, and accuracy. Example initial targets: a 100-order/1,000-operation baseline with 30 replications in under 60 seconds; then measure 1,000-order/10,000-operation and network tiers before setting their runtime promises. These are proposed targets, not current measurements.
2. Profile parsing, compilation, simulation, scoring, and persistence separately. Introduce an immutable serializable compiled representation before attempting shared compilation caches: the existing compiled model contains closures that [replication](../src/twinflow/plan/replication.py) deliberately reloads inside workers. Cache only under a key covering tenant/security scope, model, snapshot, patch, engine/plugin versions, metric/objective versions, horizon, fidelity, and seed set.
3. Reuse worker processes and batch tasks; deduplicate equivalent scenarios; implement early rejection of infeasible candidates. Add distributed experiment execution only after a single node's limits are understood. Use summaries to control event-log volume without losing the evidence required to explain decisions.
4. Add `reset(snapshot)`, `observe()`, `available_actions()`, `act(action, expected_state_version)`, and `advance(until_decision)` to a dedicated policy runtime. Explicitly separate observable state from hidden future randomness. Record action/state IDs and the exact policy artifact/version.
5. Supply deterministic baseline policies and action masks. Define decision frequency, fallback on timeout/invalid actions, simulated-time versus agent wall-time handling, and exogenous event replay. Ordinary experiments should use recorded/local policies; remote LLM round trips at every simulated event would dominate execution and complicate reproducibility.
6. Evaluate sequential policies on unseen demand/disruption scenarios; compare with heuristics and RM-06 schedules. Include simulation-model error and changed operating regimes. RL training is an optional consumer of this runtime, not evidence that the runtime is valid.
7. Introduce draft operational proposals, then human-approved writeback, then narrowly delegated actions. Use an application transaction to persist approval scope, proposal digest, expected state, expiration, and an outbox entry. A connector sends it with downstream idempotency/version controls and records a receipt. Reconcile outcomes before marking the operation confirmed.

**Proposed policy protocol:**

```python
from typing import Protocol

class Policy(Protocol):
    def decide(self, observation: dict, allowed_actions: tuple[dict, ...]) -> dict:
        """Return one allowed action using observable state only."""

def decision_boundary(runtime, policy):
    state = runtime.observe()
    actions = runtime.available_actions()
    proposed = policy.decide(state, actions)
    runtime.apply_validated(proposed, expected_state_version=state["version"])
    # Engine persists the observation/action record before advancing simulated time.
```

**Operational action envelope:**

```json
{
  "proposal_id": "proposal-41",
  "result_id": "result-98",
  "proposal_digest": "sha256:<canonical-proposal-digest>",
  "expected_operational_revision": "erp-schedule-104",
  "approval_id": "approval-19",
  "expires_at": "2026-09-12T15:00:00Z",
  "idempotency_key": "proposal-41:commit-1",
  "action": {"type": "set_order_release", "order_id": "order-17", "release_slot": "slot-25"}
}
```

At write time, revalidate the exact approved change against current state and current authority. A successful simulation is not an approval. If a downstream system lacks idempotent/versioned writes, do not promise exactly-once execution: use reconciliation and manual resolution for uncertain outcomes. Schedules already released may require a compensating action rather than rollback; physical operations cannot be undone by a database transaction.

**Tools:** reuse Temporal workers, PostgreSQL, and the existing Gym adapter after adapting it to actual sequential state. Use [Playwright](https://github.com/microsoft/playwright) for end-to-end import/review/approval journeys. Profile the Python implementation before selecting additional compute infrastructure or rewriting a hot path.

**Acceptance:** disclose benchmark conditions and cost; scale workers without changing seeded outcomes; prove action masks and stale-state checks; replay policy traces; test writeback timeout, duplicate delivery, revoked approval, intervening ERP edits, and partial downstream success. Complete a shadow period against real decisions, followed by measured bounded deployment and renewal evidence.

## Delivery workflow and definition of done

Create one GitHub epic per `RM-NN`; split each into reviewable issues with: decision being enabled, existing module touched, proposed contract, domain assumptions, fixtures, release gate, migration/compatibility impact, and owner. Use GitHub Projects to track the dependency graph and customer validation milestones. No issue is done solely because generated code passes tests that restate its implementation.

For code PRs, preserve the existing CI gates from [CI workflow](../.github/workflows/ci.yml): Ruff lint/format, strict mypy, pytest, and dependency audit. Add contract fixtures, frontend checks, property tests, and resource/performance gates only where meaningful. Lock dependencies and update them through reviewed changes; archive the environment used for published evidence. The repository currently runs CI on Python 3.12 while advertising a 3.11 runtime floor; test the supported runtime matrix explicitly before making compatibility promises.

For each milestone, require four artifacts:

1. A working scenario or reproducible failure fixture that demonstrates the user decision.
2. A versioned contract and migration notes, including which SDK/CLI/API behavior changes.
3. Independent correctness/validation evidence and the remaining fidelity limitations.
4. A short operator guide and release record linking back to its roadmap step.

The first implementable batch is RM-01 order-outcome accounting, bounded execution, result statistics, and explicit artifact directories, followed by RM-02 capsule import/export. That sequence lets subsequent agents and interfaces build on answers whose meaning is reliable.
