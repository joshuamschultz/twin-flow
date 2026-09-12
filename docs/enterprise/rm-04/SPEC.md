# RM-04 — Agent decision workspace

[Roadmap](../../enterprise-roadmap.md#rm-04)

## Requirements

- R1: Agents discover machine-readable capabilities, schema, examples, assumptions and supported actions without private imports.
- R2: Agents and operators import complete scenario content, list/retrieve immutable versions, branch a baseline with explicit changes, and export an identical portable capsule.
- R3: Building a twin from records and human answers retains draft data and identifies missing facts; it must not fabricate a production model or mark drafts runnable.
- R4: Evaluations are asynchronous, bounded jobs with retained evidence, status, errors and idempotent requests. A result identifies its scenario and carries assumptions and statistical meaning.
- R5: Compare completed results only from compatible scenarios/experiment settings and expose differences without claiming causal or guaranteed operational outcomes.
- R6: Modern responsive operator workspace supports sample loading, file/paste import, overview/process view, explicit assumptions, run and results, downloads and agent API discovery. All actions use the same API as agents.
- R7: Transport adapters depend on application contracts; the application depends on scenario/experiment contracts and repository interfaces. No frontend-only calculation of business KPIs.
- R8: Local-first use requires no external LLM. Remote deployment security remains explicit; no production-write actions in evaluation.

## Components

- C1 application contracts and scenario adapter: only module consuming capsule implementation details.
- C2 local durable repository: SQLite metadata and immutable artifact content, injected paths.
- C3 workspace service: import/draft/version/evaluation/compare orchestration and capability discovery.
- C4 FastAPI workspace router under /api/workspace, typed request/response contracts.
- C5 public SDK and optional MCP transport delegating to C3/C4.
- C6 React workspace components: resource library, selected-scenario workspace, import dialog, results and agent integration panel.

## Plan and acceptance

1. Write repository/application tests for invalid IDs, isolated versions, exports, draft questions, idempotency and job error persistence.
2. Implement application contracts and tests before router wiring; run API import/run/export workflows on actual examples.
3. Implement bounded typed HTTP API and SDK/MCP; preserve existing legacy endpoints.
4. Build operator UI against explicit contracts; responsive/keyboard states and actionable errors required.
5. Validate production frontend build and browser journeys, independently review all changes and write REVIEW.md and USER-GUIDE.md.

## Limits

A local service is not an accredited enterprise deployment. The engine only advertises profile features actually implemented. Imported free-text notes are evidence, never trusted instructions. Forecasts remain provisional until calibrated against actual operations.
