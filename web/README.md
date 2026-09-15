# twinflow web

The Twinflow web package is the local operator workspace for the integrated
enterprise build. It uses the same workspace API as the Python SDK and MCP
adapter to import or construct scenario capsules, validate and branch them, run
bounded evaluations, inspect evidence, compare results, review schedules, and
export scenarios. It covers manufacturing, office workflows, and supply
networks through the domain adapters registered by the API.

The UI is an operator surface for a dedicated local workspace. It does not
contain an LLM and it does not write to external systems: proposals are
reviewed through the API's evidence-bound dry-run flow. See the [workspace
guide](../docs/enterprise/README.md) and [agent guide](../docs/enterprise/AGENT-GUIDE.md)
for the shared contracts.

## Prerequisites

Use Node.js 22.12 or newer.

Install the Python service from the repository root, then start it on the local
operator address:

```bash
python -m twinflow.service.serve --host 127.0.0.1 --port 8000 \
  --models-root examples --workspace-root .twinflow-workspace
```

The service should be running before the Vite app. The development server
proxies `/api` to `http://127.0.0.1:8000`; no CORS setting or browser-side API
secret is needed for this local setup.

If port 8000 is already used by another program (the API then fails to start,
or every workspace call in the browser returns a 404 or 500 from the wrong
server), start the API on a free port and point the proxy at it:

```bash
python -m twinflow.service.serve --host 127.0.0.1 --port 8010 \
  --models-root examples --workspace-root .twinflow-workspace
TWINFLOW_API_TARGET=http://127.0.0.1:8010 npm run dev
```

## Development

```bash
cd web
npm ci
npm run dev
```

Open the printed local URL, typically `http://127.0.0.1:5173`. Use `npm run
build` for the type check and production bundle, or `npm run preview` to serve
the built files locally. Preview does not include the development proxy, so a
reverse proxy or same-origin API is required.

## Workspace flow

1. Discover capabilities, schemas, examples, and tool contracts.
2. Import a capsule or use the draft intake to record facts and unanswered
   questions.
3. Validate, save, branch, and export a versioned scenario.
4. Evaluate asynchronously, then inspect bounded evidence and compare a
   candidate with its baseline.
5. Review schedules or proposals with the recorded assumptions and limits.

The legacy manufacturing model picker and floor view remain available for
`model.yaml` examples, but the capsule workspace is the release's shared
agent/operator boundary.

## Structure

The main workspace client and views live under `src/workspace/`; the older
manufacturing views and typed API helpers remain alongside them. Browser
journeys are under `tests/browser` and use a disposable local workspace.

## Design notes

- Confidence ranges and evidence are shown with the assumptions that produced
  them; a result is model evidence rather than a commitment.
- Scenario edits are validated and persisted with provenance and digests.
- Agents propose facts and changes; the deterministic service executes bounded
  work and records the result. Natural-language reasoning belongs to the host
  agent.
