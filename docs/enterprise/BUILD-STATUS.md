# Enterprise build status

Integration branch: `enterprise/integration-docs`. This page records the integrated implementation slice, not enterprise readiness. Stable goals remain in the [roadmap](../enterprise-roadmap.md); rationale remains in the [playbook](../enterprise-build-playbook.md). Root will append final aggregate test totals.

Principles: agent-first contracts; simple modules; explicit domain boundaries; operator UI over the same application API; no hidden production-readiness claims.

[Roadmap](../enterprise-roadmap.md) · [Build playbook](../enterprise-build-playbook.md)

Each epic must include SPEC.md, implementation, independent review evidence, and user documentation. Customer-data validation and external operational accreditation cannot be replaced with synthetic tests.

| Epic | Scope | Status |
|---|---|---|
| RM-01 | Bounded runs, outcomes, evidence and concurrency-safe artifacts | Customer acceptance and production observability |
| RM-02 | Versioned capsules, digest/export, safe YAML, schema, edits, legacy import and dispatch | Migration breadth, archive format and customer onboarding |
| RM-03 | Manufacturing simulation plus typed capsule compilation | Calendars, resource identity, WIP/material fidelity and credible dates |
| RM-04 | Workspace REST, SDK, MCP, draft intake, jobs, compare, evidence and React UI | Independent clients, verified identity/RBAC and customer evidence; see [review](rm-04/REVIEW.md) |
| RM-05 | Event ingestion, snapshots, freshness and forecast backtest | Production connectors and held-out calibration |
| RM-06 | Strict parser, verified baseline and optional CP-SAT backend/CLI/API | Broad objectives, scale proof and operational validation |
| RM-07 | Registered office adapter with cases, documents, approvals and bounded rework | Customer semantics and identity |
| RM-08 | Registered supply-chain adapter with inventory, BOMs, qualifications and gates | Supplier data, network scale and calibrated lead times |
| RM-09 | Settings, health, workspace lock, SQLite, backup/restore and bounded workers | SSO, RLS, secrets, distributed workers and SLO evidence |
| RM-10 | Policy runtime, evidence-bound proposals, approval outbox and forced dry-run sink | External connector, production authorization and writeback |

## Operating boundary

Install with `.[api,agent,scheduling,dev]`, serve with `python -m twinflow.service.serve`, and run the web package with `npm ci && npm run dev`. The trusted deployment is one dedicated workspace; the shared token does not verify human identity or provide RBAC. There is no production SSO/RLS, distributed worker deployment, giant-model scale proof, or customer calibration. Agents own natural-language reasoning and twin construction from supplied facts; the engine has no built-in LLM and never invents unknown facts.

The operator API exposes proposal approval and dry-run delivery. MCP exposes proposal creation, but not approval or delivery. All action results are evidence-bound and no external system is changed. CP-SAT is a narrow optional backend over the published finite scheduling grammar.

## Review checks

Docs were checked against source routes in `src/twinflow/service`, SDK/MCP methods, CLI parsers, and UI calls in `web/src/workspace/api.ts`. All RM links resolve to existing epic documents. No source, test, web, or application files are changed by this documentation branch.
