# RM-10 user guide

## Recorded local policy execution

Implement `Policy.decide(observation, allowed_actions)` and return one item from the
provided mask. Pass `PolicyRuntime(policy, "artifact-or-version")` to
`RunDriver.run(..., decision_runtime=runtime)`. Each real queue decision records a
state version, observation digest, action, time, location, fallback reason, and policy
artifact in `decision_trace.json`.

`observe()`, `available_actions()`, and `act(action, expected_state_version)` expose the
interaction contract while the policy callback is running. The current implementation
is synchronous and local: it is policy injection into a complete run, not a remotely
pausable simulator session. Dispatch changes apply at queue-selection boundaries and do
not interrupt work that has already started.

For deterministic replay, create `ReplayPolicy(previous_runtime.trace)`, wrap it in a
new runtime with `fallback_on_error=False`, and run the same model, plan, seed, and
replication index. Replay raises `ReplayMismatchError` if observable state drifts.

## Controlled actions

Create a proposal with `Proposal.create`; its canonical SHA-256 digest binds the action,
proposal ID, scenario ID, result ID, and expected operational revision. Register an
`Approval` with an aware expiry, approver identity, exact digest, and allowed action
types. `TransactionalOutbox.submit` validates and consumes that approval in the same
SQLite transaction that creates the idempotent outbox row.

Call `dispatch_pending` with a configured sink and the current operational revision.
A revision mismatch moves the entry to `conflict` without calling the sink. The included
`DryRunSink` records deterministic receipts and performs no external operation. A crash
after claiming a delivery can leave it `delivering`; after restart, call `recover()` to
return those rows to `pending`. Downstream consumers must honor the supplied idempotency
key because a crash can occur after a downstream write but before the receipt commits.
An unconfirmed receipt remains `uncertain` until `reconcile` records the outcome.

This is a local single-database control boundary. It does not supply authentication,
authorization policy, distributed consensus, secret management, or an external-system
connector.

## Benchmark

Run:

```console
python -m twinflow.benchmark examples/active-control/model.yaml \
  --plan examples/active-control/plan.csv \
  --output benchmark.json --artifact-dir benchmark-artifacts \
  --repetitions 3 --seed 42 \
  --max-sim-time 1000000 --max-events 100000 --max-wall-seconds 30
```

The JSON reports its exact inputs, finite bounds, environment, wall time, event count,
simulated time, retained artifact bytes, termination reasons, and simple cost proxies.
Results describe only that invocation on that host.
