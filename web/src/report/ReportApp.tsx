import { StatesView } from "../decision/states/StatesView";
import { ImproveView } from "../decision/improve/ImproveView";
import { ProjectView } from "../decision/project/ProjectView";
import type {
  ImproveViewModel,
  ProjectViewModel,
  StatesViewModel,
} from "../decision/types";

// Picks the view from the inlined view model (DESIGN.md 11.2).
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

export function ReportApp({ model }: { model: AnyViewModel | null }) {
  if (!model) {
    return (
      <main className="tf-root tf-wrap">
        <p>This report has no data. Make it again with twinflow.</p>
      </main>
    );
  }
  const view = model.view ?? "race";
  if (view === "states")
    return <StatesView model={model as unknown as StatesViewModel} />;
  if (view === "improve")
    return <ImproveView model={model as unknown as ImproveViewModel} />;
  if (view === "project")
    return <ProjectView model={model as unknown as ProjectViewModel} />;
  return (
    <main className="tf-root tf-wrap" data-view={view}>
      <h1>{view === "race" ? "Policy race" : view}</h1>
    </main>
  );
}
