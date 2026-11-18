// Picks the view from the inlined view model (DESIGN.md 11.2).
import { EmptyState } from "../decision/primitives";
import type {
  RaceViewModel,
  RunViewModel,
  SweepViewModel,
} from "../decision/types";
import { ShellContext } from "../decision/viewState";
import { RaceView } from "../decision/views/RaceView";
import { RunView } from "../decision/views/RunView";
import { SweepView } from "../decision/views/SweepView";

export type AnyViewModel = { view?: string; [key: string]: unknown };

export function readModel(doc: Document): AnyViewModel | null {
  const text = doc.getElementById("twinflow-data")?.textContent ?? "";
  if (!text.trim() || text.includes("__TWINFLOW_DATA__")) return null;
  try {
    return JSON.parse(text) as AnyViewModel;
  } catch {
    return null;
  }
}

function Missing({ title, body }: { title: string; body: string }) {
  return (
    <main className="tf-root tf-wrap" style={{ padding: "48px 32px" }}>
      <EmptyState title={title} body={<p>{body}</p>} />
    </main>
  );
}

export function ReportApp({ model }: { model: AnyViewModel | null }) {
  if (!model) {
    return (
      <Missing
        title="This report has no data."
        body="Make it again with twinflow."
      />
    );
  }
  const view = model.view ?? "race";
  return (
    <ShellContext.Provider value="offline">
      {view === "race" ? (
        <RaceView model={model as unknown as RaceViewModel} />
      ) : view === "run" ? (
        <RunView model={model as unknown as RunViewModel} />
      ) : view === "sweep" ? (
        <SweepView model={model as unknown as SweepViewModel} />
      ) : (
        <Missing
          title={`This file holds a ${view} view.`}
          body="This version of the report cannot show it yet. Update twinflow and make the file again."
        />
      )}
    </ShellContext.Provider>
  );
}
