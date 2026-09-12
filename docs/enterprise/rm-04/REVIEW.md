# RM-04 review and readiness

The integrated slice provides `Workspace`, typed FastAPI routes under `/api/workspace`, `TwinflowClient`, MCP discovery/import/branch/evaluate/poll/compare tools, draft intake, evidence queries, event and snapshot tools, strict scheduling submission, and the React workspace. Application methods own validation and job orchestration; transports delegate to them.

The connected path is discover capabilities/schema → collect facts and answer explicit questions → validate → import → branch → evaluate with bounded settings → poll → query evidence → compare → export. Scenario IDs, parent IDs, capsule digests, job IDs, seeds, assumptions, and timestamps provide the review trail.

Acceptance evidence: public calls use documented routes and the SDK; request models reject unknown fields and bound inputs; drafts preserve facts and unresolved questions; jobs persist, deduplicate by request key, cancel, and expose terminal errors; compare enforces same-domain compatibility; evidence is bounded and topic-scoped; UI and MCP consume the same application contract.

Residual scope: this is a local alpha. The shared token is not verified human identity and there is no tenant RBAC, SSO, RLS, distributed worker fleet, or production artifact store. Forecasts have not been calibrated against customer operations. MCP does not approve or deliver actions; operator action is a fixed dry-run sink. RM-04 has an implemented slice, while independent clients, remote security, production persistence, and customer evidence remain roadmap gates.
