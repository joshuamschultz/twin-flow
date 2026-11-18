import React from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import "../decision/tokens.css";
import "../decision/fonts/fonts.css";
import "../decision/decision.css";
import "./report.css";
import { applyThemeFromUrl } from "../decision/Page";
import { ReportApp, readModel } from "./ReportApp";

applyThemeFromUrl();
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ReportApp model={readModel(document)} />
  </React.StrictMode>,
);
