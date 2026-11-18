// Fixtures page for the explore components (B-15 to B-17). Mounts one component with the
// golden race data. Pick it with ?c=compare or ?c=whatif.
import React from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import "../decision/tokens.css";
import "../decision/fonts/fonts.css";
import "../decision/decision.css";
import golden from "../../../docs/design/race-view-model.full.json";
import type { RaceViewModel } from "../decision/types";
import { CompareTable } from "../decision/CompareTable";

const model = golden as unknown as RaceViewModel;
const which = new URLSearchParams(window.location.search).get("c") ?? "compare";

function Fixture() {
  return (
    <main className="tf-root tf-wrap" data-fixture={which}>
      {which === "compare" ? (
        <CompareTable
          policies={model.policies}
          runs={model.about.episodes}
          horizonDays={model.about.horizon_days}
        />
      ) : (
        <p>Unknown fixture: {which}</p>
      )}
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Fixture />
  </React.StrictMode>,
);
