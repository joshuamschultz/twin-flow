// Mounts the replay parts alone with the data the test put in window.__REPLAY__.
import React from "react";
import ReactDOM from "react-dom/client";
import "../../../src/index.css";
import "../../../src/decision/tokens.css";
import "../../../src/decision/fonts/fonts.css";
import "../../../src/decision/decision.css";
import { TooltipHost } from "../../../src/decision/Tooltip";
import { ReplayLanes } from "../../../src/decision/ReplayLanes";
import { StripStack } from "../../../src/decision/ReplayStrips";
import { replayRuns } from "../../../src/decision/replay";
import type { RaceViewModel } from "../../../src/decision/types";

type Fixture = { kind: "strips" | "lanes"; model: RaceViewModel };

function App({ data }: { data: Fixture }) {
  const { model } = data;
  const runs = replayRuns(model);
  if (data.kind === "lanes") {
    return (
      <main className="tf-root" data-ready="replay" style={{ padding: 24 }}>
        <ReplayLanes
          runs={runs}
          products={model.products}
          families={model.families}
          economics={model.economics}
        />
      </main>
    );
  }
  return (
    <main className="tf-root" data-ready="replay" style={{ padding: 24 }}>
      {runs.map((r) => (
        <section key={r.id} data-lane={r.id} style={{ marginBottom: 32 }}>
          <h3>{r.label}</h3>
          <StripStack
            label={r.label}
            run={r.run}
            products={model.products}
            families={model.families}
            economics={model.economics}
            day={9}
          />
        </section>
      ))}
    </main>
  );
}

const data = (window as unknown as { __REPLAY__: Fixture }).__REPLAY__;
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <TooltipHost>
      <App data={data} />
    </TooltipHost>
  </React.StrictMode>,
);
