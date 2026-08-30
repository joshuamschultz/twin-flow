# twinflow web

React + TypeScript + Vite front end for twinflow, a manufacturing digital-twin
engine. This is the alpha UI: model picker, floor map, and three work tabs
(Run, Sweep, Optimize) driven entirely by the local FastAPI backend.

## Prerequisites

The API must be running on `http://localhost:8000` before you use the app —
the Vite dev server proxies `/api/*` to it. From the repo root:

```bash
python -m twinflow.service.serve
# or, equivalently:
uvicorn twinflow.service.app:create_app --factory --host 0.0.0.0 --port 8000
```

## Development

```bash
cd web
npm install
npm run dev
```

Open the printed local URL (typically `http://localhost:5173`). The dev
server proxies any `/api/...` fetch to the backend on `:8000`, so no CORS
configuration or env vars are needed locally.

## Build

```bash
npm run build
```

Runs a type check (`tsc --noEmit`) then produces a production bundle in
`dist/`. `npm run preview` serves that bundle locally if you want to sanity
check it (the `/api` proxy only applies to `npm run dev`, so `preview`
expects the API to be reachable at the same origin, or you serve `dist/`
behind a proxy that forwards `/api`).

## Structure

```
src/
  api.ts                 typed fetch client + response/request types for every endpoint
  App.tsx                shell: header, model picker, left nav, tab routing
  index.css              the "BlackArc" theme — CSS variables + component styles
  components/
    FloorTab.tsx          fetches /floor, renders FloorMap
    FloorMap.tsx          React Flow graph of locations + stocks
    floorLayout.ts        layered left-to-right layout from routing order
    RunTab.tsx             single-scenario run + KPI cards
    SweepTab.tsx            one lever, several candidate values, side by side
    OptimizeTab.tsx        objective/optimizer search + convergence chart
    ConfidenceBand.tsx     the range-bar visual used for every interval
    UtilizationList.tsx    per-cell utilization bars with confidence overlay
    Spinner.tsx             small inline loading indicator
```

## Design notes

- Confidence intervals are a first-class visual (a filled range with a mean
  tick), not an afterthought — the product's stance is honest ranges over
  point estimates.
- The Sweep tab intentionally does not highlight a "winner": the user reads
  the bands and decides.
- The Optimize tab does pick a best score (that's the point of optimization),
  but says so plainly: "the twin scores; you decide."
