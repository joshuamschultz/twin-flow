# Front end

A React + TypeScript + Vite app in `web/` that manages the whole twin from the browser:
map the floor, run it, sweep a lever, and optimize — with the confidence band as the
central visual throughout.

## Run it (development)

Two terminals.

**1. Start the API** (serves the data the UI reads):

```bash
pip install -e ".[api]"
twinflow serve --port 8000
```

**2. Start the front end:**

```bash
cd web
npm install
npm run dev
```

Open the URL Vite prints (default `http://localhost:5173`). The dev server proxies
`/api` to `http://localhost:8000`, so both must be running.

## Build (production)

```bash
cd web
npm run build      # tsc --noEmit && vite build  → web/dist/
npm run preview    # serve the built dist/ locally
```

## What the UI does

| Area | Reads / posts | Shows |
|---|---|---|
| **Model picker** | `GET /api/models` | choose the floor the whole app works on |
| **Floor map** | `GET /api/models/{name}/floor` | a React Flow graph, laid out left-to-right along routing order; location vs stock nodes are visually distinct |
| **Run** | `POST /api/run` → poll | on-time % as a mean with a lo–hi band, run/setup hours, utilization-by-cell bars |
| **Sweep** | `POST /api/sweep` → poll | one lever's candidate values side by side, each with its band (no winner is highlighted) |
| **Optimize** | `POST /api/optimize` → poll | the winning scenario + its band, plus a convergence chart of the search — "the twin scores; you decide" |

The theme (a light/dark "BlackArc" palette) is remembered in the browser.

## Files

`src/api.ts` is the typed client mirroring the [API contract](api.md). Tabs live in
`src/components/` (`FloorTab`, `RunTab`, `SweepTab`, `OptimizeTab`), with the shared
range visual in `ConfidenceBand.tsx` and the floor layout in `floorLayout.ts`.
