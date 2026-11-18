# Front end

![Three floating glass panels showing a floor map, a band chart and bars](images/headers/frontend.webp)

A React + TypeScript + Vite app in `web/`. It has two parts. The first part is the report
pages. `twinflow` writes each one as a single HTML file that works offline. The second part
is the workspace app, which has the DECISIONS pages. Both parts show the same views.

## Run the workspace app (development)

Two terminals.

**1. Start the API** (serves the data the app reads):

```bash
pip install -e ".[api]"
twinflow serve --port 8000 --workspace-root .twinflow-workspace
```

**2. Start the front end:**

```bash
cd web
npm install
npm run dev
```

Open the URL Vite prints (default `http://localhost:5173`). The dev server proxies
`/api` to `http://localhost:8000`, so both must be running. If port 8000 is taken, see
[`web/README.md`](../web/README.md).

## Build

```bash
cd web
npm run build         # tsc --noEmit && vite build, output in web/dist/
npm run build:report  # rebuilds the report template every HTML page uses
npm run preview       # serve the built dist/ locally
```

Run `npm run build:report` after you change anything in `web/src/decision/`. The template
is `src/twinflow/report/assets/report.template.html`. A test fails when the template is older
than the source.

## Report pages

Open a page in a browser. Each section answers one question. The question is the heading.

| Page | Written by | Sections, and what each answers |
|------|------------|---------------------------------|
| `race.html` | `twinflow policies race`, `twinflow improve` | **Why it wins**: where the extra money comes from, and when the pick pulls ahead of today. **All rules**: every rule side by side. **Compare two**: two rules side by side. **What if**: your own cost numbers. **Floor replay**: watch the floor run. **Details**: how this was made. |
| `report.html` | `twinflow run`, `twinflow report` | **Orders at risk**: which orders are late, and why. **Choke point**: where the line chokes. **Best fix**: which one change helps most. **Details**: run facts and problems. |
| `sweep.html` | `twinflow balance`, `twinflow optimize` | **Changes**: which change helps most. **Details**: every point tested. |
| `states.html` | `twinflow states --html`, `twinflow markov --html` | **Map**: which situations the plant lives in, and which ones cost money. **Money**: where the money goes. **Leaks**: where the plant loses money. **Details**: run facts and warnings. |
| `improve.html` | `twinflow improve` | **Proof**: can you trust the result, from a checklist of passes and fails, and how the two rules did. **Details**: run facts and problems. |
| `project.html` | `twinflow extensions check --html` | **Fix first**: problems to fix. **The twin**: what the twin holds and what things cost. **Rules**: which rules can race, and how they are scored. |

The commands and their flags are in [`docs/cli.md`](cli.md#report-pages).

## DECISIONS pages in the workspace

The workspace app has a DECISIONS area. It lists every published view in one place:

- **Rule races**: each race, with its rules and the pick.
- **Runs**: each run viewer page.
- **Sweeps**: each sweep page.
- **States**: each state map.
- **Improve**: each improve result.
- **Projects**: each project check.

A view reaches DECISIONS when it is published into a workspace folder. The folder is the
one `twinflow serve --workspace-root` reads. There are two ways to publish:

- Pass `--workspace <folder>` to `twinflow policies race`.
- Set `TWINFLOW_WORKSPACE_ROOT` to the folder. Then the run viewer, sweep, and race
  reports are published as they are written.

```bash
export TWINFLOW_WORKSPACE_ROOT=.twinflow-workspace
twinflow policies race examples/molding-press --episodes 20
twinflow serve --workspace-root .twinflow-workspace
```

## Files

- `web/src/api.ts`: the typed client for the API. See [API contract](api.md).
- `web/src/decision/`: the report views, one per page, and the shared parts they use.
  `tokens.json` holds the shared colors and fonts.
- `web/src/workspace/`: the workspace app. `DecisionPages.tsx` holds the DECISIONS tabs.
- `web/src/components/`: the floor map (`FloorMap.tsx`) and its layout (`floorLayout.ts`).
