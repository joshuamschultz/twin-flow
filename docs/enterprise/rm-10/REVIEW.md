# RM-10 implementation review

## Requirement coverage

- The dispatch hook operates inside `Location._dispatch_order`, so a policy action
  changes the current eligible queue and future selections. Trace replay compares a
  digest of the observable state before returning the recorded action.
- Proposals are re-canonicalized at submission. SQLite `BEGIN IMMEDIATE` serializes the
  idempotency check, approval check, approval consumption, and outbox insert. Rows and
  receipts survive process restart.
- Dispatch checks operational revision before the sink call. Delivery claim state makes
  interruption visible; recovery and uncertain receipt reconciliation are explicit.
- The only concrete sink is dry-run. The benchmark retains artifacts and reports finite
  limits and the measured environment.

## Review findings and limits

The policy surface is an in-process callback, so interactive remote stepping and durable
policy-session resumption remain outside this milestone. Failed policy attempts are not
persisted unless fallback is enabled; fallback records the failure reason. The SQLite
implementation assumes one local database and relies on downstream idempotency during
crash recovery. It does not authenticate approver identities or configure an external
sink. Revision conflicts require a new proposal and approval; they are intentionally not
retried automatically.

Python 3.11+ typing, immutable boundary records, narrow exception classes, connection
cleanup, parameterized SQL, and strict static analysis follow the project Python
patterns. Tests cover forward dispatch, replay, stale/masked actions, tamper/expiry/
revocation/scope rejection, durable idempotency, restart recovery, conflicts, uncertain
receipts, reconciliation, and bounded benchmark output.
