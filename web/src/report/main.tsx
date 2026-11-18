import React from "react";
import ReactDOM from "react-dom/client";
import "../index.css";
import "../decision/tokens.css";
import "../decision/fonts/fonts.css";
import "../decision/decision.css";
import { ReportApp, readModel } from "./ReportApp";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ReportApp model={readModel(document)} />
  </React.StrictMode>,
);
