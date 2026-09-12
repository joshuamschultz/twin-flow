# RM-10: policy interaction and controlled action foundations

## Requirements

- RM10-R1: A local recorded policy receives only observable queue state and an action
  mask at real dispatch boundaries. A valid action changes only future job selection.
- RM10-R2: Every boundary has a monotonic state version. Stale, malformed, masked, or
  wrong-location actions fail closed. Applied and fallback actions are retained as evidence.
- RM10-R3: Seeded execution plus an exact action trace reproduces semantic event and
  decision results. Trace replay rejects incompatible policy/trace identity.
- RM10-R4: Ordinary runs remain unchanged when no policy runtime is supplied. A policy
  failure uses an explicit configured fallback and records its reason.
- RM10-R5: Operational proposals have canonical digests tied to scenario/result IDs and
  expected operational revision. Approval names identity, scope, digest, and expiry.
- RM10-R6: Dispatch uses a transaction-safe outbox abstraction with idempotency,
  downstream revision checks, receipts, and explicit uncertain/reconciliation states.
- RM10-R7: The only included sink is deterministic and dry-run. No external operational
  system is mutated by simulation/evaluation code.
- RM10-R8: A reproducible benchmark reports conditions, environment, wall time, events,
  replications, horizon, artifact bytes, and cost proxies without claiming scale.

## Public contracts

- `PolicyRuntime(policy, policy_artifact, fallback="fifo")`; pass it as
  `RunDriver.run(..., decision_runtime=runtime)`. It exposes `reset`, `observe`,
  `available_actions`, `act(action, expected_state_version)`, and a retained `trace`.
- `ReplayPolicy(trace)` returns recorded actions in boundary order and validates the
  observation digest before applying each action.
- The supported action is `DispatchAction(location_id, policy)` where policy is one of
  `fifo`, `edd`, `spt`, or `critical_ratio`.
- `Proposal.create(...)`, `Approval`, `TransactionalOutbox.submit(...)`,
  `dispatch_pending(sink, current_operational_revision=...)`, and `reconcile(...)` define the
  controlled-action seam in `twinflow.actions`.
- `python -m twinflow.benchmark MODEL --plan ... --repetitions 2 --artifact-dir ...
  --max-sim-time ... --max-events ... --max-wall-seconds ... --output result.json`
  runs the local benchmark.

## Components and tasks

1. Add typed policy observations, actions, errors, traces, replay, and controller protocol.
2. Hook policy decisions into `Location` before dispatch ordering and pass the runtime
   through `RunDriver`; retain trace evidence beside run artifacts.
3. Add immutable controlled-action envelopes and a durable SQLite transactional outbox
   reference implementation with a dry-run sink.
4. Add a benchmark CLI with explicit execution bounds and environment evidence.
5. Test forward-only intervention, masks, stale actions, deterministic replay, proposal
   tampering, expiry/revocation/scope, duplicate dispatch, revision conflict, uncertain
   receipt, and reconciliation.
6. Run focused/full tests, Ruff, and strict mypy; write review and operator guidance.

## Acceptance

- Changing FIFO to EDD at a real queue boundary changes the first eligible order while
  leaving already-started work unchanged.
- A stale state version and unsupported policy raise typed errors.
- A replayed trace produces equal semantic event rows and an equal decision trace.
- Duplicate outbox submission or dispatch does not call the sink twice.
- Expired/revoked/wrong-scope approval and intervening operational revision prevent send.
- An uncertain send stays unresolved until explicit reconciliation records a receipt.
- Benchmark output includes declared bounds and observed evidence and is presented as a
  measurement rather than an enterprise performance promise.

This milestone supplies local software foundations. It does not establish distributed
scale, safe customer writeback, model validity, shadow-period performance, or renewal
evidence.
