// Fixtures page for the tuning and details components (TF-006 B-23, B-24).
// Open /fixtures-details.html?c=<name>&theme=light|dark. The model comes from
// window.__MODEL__ (set by the Playwright test), so one page serves every fixture.
import React from "react";
import ReactDOM from "react-dom/client";
import "../../index.css";
import "../tokens.css";
import "../fonts/fonts.css";
import "../decision.css";
import "../details.css";
import { TuningCurve } from "../TuningCurve";
import { TuningTable } from "../TuningTable";
import { TuningView } from "../TuningView";
import { tuningRows } from "../tuningModel";
import type { RaceViewModel } from "../types";

declare global {
  interface Window {
    __MODEL__?: RaceViewModel;
  }
}

const params = new URLSearchParams(location.search);
const theme = params.get("theme");
if (theme) document.documentElement.dataset.theme = theme;

function Fixture({ model }: { model: RaceViewModel }) {
  switch (params.get("c")) {
    case "tuning-curve": {
      const policy = params.get("policy") ?? model.recommendation.policy;
      const row = model.tuning.find((t) => t.policy === policy)!;
      return <TuningCurve tuning={row} label={policy ?? ""} />;
    }
    case "tuning-table":
      return <TuningTable rows={tuningRows(model)} />;
    case "tuning":
      return <TuningView model={model} />;
    default:
      return <p>Unknown fixture.</p>;
  }
}

const model = window.__MODEL__;
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <main
      className="tf-root tf-wrap"
      style={{ maxWidth: 1180, margin: "0 auto", padding: 24 }}
    >
      {model ? <Fixture model={model} /> : <p>No model.</p>}
    </main>
  </React.StrictMode>,
);
