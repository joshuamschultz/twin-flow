# RM-04 user guide — agent decision workspace

RM-04 connects one deterministic application layer to FastAPI, the Python `TwinflowClient`, MCP stdio tools, and the React workspace. Start with `python -m twinflow.service.serve`; browser and agents call the same `/api/workspace` routes.

Use `/capabilities` and `/schema` first. Load an example or submit a YAML/JSON capsule. For incomplete discovery, save facts with `/drafts`, answer question IDs with `/drafts/{id}/answers`, and keep the brief until missing facts are resolved. `/validate` reports issues without saving or running. Imports receive an immutable scenario ID and digest; branches are validated children.

Evaluations are bounded asynchronous jobs. Submit positive replications, a nonnegative 32-bit seed, and a request key. Poll `/jobs/{id}` to terminal status, then retrieve paginated evidence. Results preserve assumptions, per-replication outcomes, intervals, censored counts, and interpretation. Compare requires compatible completed same-domain jobs.

The UI provides sample loading, paste/file import, scenario graph, draft questions, run/results, evidence, and agent integration views. It does not calculate business KPIs in the browser. The local service stores metadata and artifacts in `.twinflow-workspace`; use admin backup/restore for a controlled copy.

This is an agent contract slice, not a hosted multi-tenant product. One trusted workspace, shared token middleware, local SQLite, bounded in-process workers, no built-in LLM, and no production writeback are intentional limits. Operator approval and dry-run delivery are API routes; MCP only creates proposals.
