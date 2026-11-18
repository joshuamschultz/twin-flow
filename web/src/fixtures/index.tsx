// The one fixtures page for component tests (BUILD-PLAN "Test approach").
// Open web/fixtures/index.html?c=<name>. The test may put a race view model on
// window.__TF_FIXTURE__ before the page loads; without one the golden full model is used.
import React from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import "../decision/tokens.css";
import "../decision/fonts/fonts.css";
import "../decision/decision.css";
import golden from "../../../docs/design/race-view-model.full.json";
import { TooltipHost } from "../decision/Tooltip";
import type { RaceViewModel } from "../decision/types";
import { CompareFixture, WhatIfFixture } from "./explore";
import { LanesFixture, StripsFixture } from "./replay";
import { StateMapFixture } from "./stateMap";

const FIXTURES: Record<
  string,
  (p: { model: RaceViewModel }) => React.ReactNode
> = {
  compare: CompareFixture,
  whatif: WhatIfFixture,
  statemap: StateMapFixture,
  strips: StripsFixture,
  lanes: LanesFixture,
};

const which = new URLSearchParams(window.location.search).get("c") ?? "";
const model = ((window as unknown as { __TF_FIXTURE__?: RaceViewModel })
  .__TF_FIXTURE__ ?? golden) as RaceViewModel;
const Fixture = FIXTURES[which];

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <TooltipHost>
      <main
        className="tf-root tf-wrap"
        data-ready={which}
        style={{ padding: "24px 32px" }}
      >
        {Fixture ? (
          <Fixture model={model} />
        ) : (
          <p>
            Unknown fixture “{which}”. Use one of:{" "}
            {Object.keys(FIXTURES).join(", ")}.
          </p>
        )}
      </main>
    </TooltipHost>
  </React.StrictMode>,
);
