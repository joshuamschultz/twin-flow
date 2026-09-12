# RM-04 user guide — agent decision workspace

RM-04 connects one deterministic application layer to FastAPI, the Python `TwinflowClient`, MCP stdio tools, and the React workspace. Start with `python -m twinflow.service.serve`; browser and agents call the same `/api/workspace` routes.

Use `/capabilities` and `/schema` first. Load an example or submit a YAML/JSON capsule. For incomplete discovery, save facts with `/drafts`, answer question IDs with `/drafts/{id}/answers`, and keep the brief until missing facts are resolved. `/validate` reports issues without saving or running. Imports receive an immutable scenario ID and digest; branches are validated children.

Evaluations are bounded asynchronous jobs. Submit positive replications, a nonnegative 32-bit seed, and a request key. Poll `/jobs/{id}` to terminal status, then retrieve paginated evidence. Results preserve assumptions, per-replication outcomes, intervals, censored counts, and interpretation. Compare requires compatible completed same-domain jobs.

## Browser steps

1. Start the API, then run `cd web && npm ci && npm run dev` and open the displayed local URL.
2. In **Scenario library**, choose **Load sample** for an included example, or choose **Import scenario**, enter a name, paste YAML/JSON or select a file, and submit **Import**. Import errors remain beside the form for correction.
3. Select the scenario card to open **Overview**. Use **Process** to inspect nodes and edges, **Source** to inspect capsule content, and **Assumptions** to review provisional inputs.
4. Choose **Branch**, edit the capsule content and name, then submit the branch. The new scenario appears as a child with its own digest.
5. Select **Run experiment**, choose the replication count, and wait for the job status. Open **Results** for outcomes and intervals, **Evidence** for retained rows, and **Download** for the result or capsule.
6. In the experiment view choose a completed baseline and candidate, then press **Compare**. Read the displayed candidate-minus-baseline interpretation together with assumptions and outcome status.
7. Open **Agent integration** to view the capability/schema example and API contract. The UI uses the same service routes as SDK and MCP clients and does not calculate business KPIs in the browser.

The local service stores metadata and artifacts in `.twinflow-workspace`; use admin backup/restore for a controlled copy.

This is an agent contract slice, not a hosted multi-tenant product. One trusted workspace, shared token middleware, local SQLite, bounded in-process workers, no built-in LLM, and no production writeback are intentional limits. Operator approval and dry-run delivery are API routes; MCP only creates proposals.
