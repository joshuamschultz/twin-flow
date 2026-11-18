// DESIGN.md 2.2: the DECISIONS pages of the workspace. A list per view, and one page per
// view that mounts the shared view component with shell="workspace".
// A new view plugs in with one line in DECISION_VIEWS.
import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { PageActionsContext } from "../decision/Page";
import { EmptyState, Section, StatusPill } from "../decision/primitives";
import type { RaceViewModel } from "../decision/types";
import { ShellContext } from "../decision/viewState";
import { RaceView } from "../decision/views/RaceView";
import { decisionApi, download, type DecisionRow } from "./api";

export type DecisionKind = {
  /** the page id of one view: ?page=race&id=<id> */
  kind: string;
  /** the page id of the list: ?page=races */
  plural: string;
  /** nav label */
  label: string;
  /** nav icon name (Icons.tsx) */
  icon: string;
  /** the question the list answers */
  question: string;
  /** null until the view exists: the list shows a "coming" note */
  render: ((model: unknown) => ReactNode) | null;
  /** what "coming" says */
  coming?: string;
};

export const DECISION_VIEWS: DecisionKind[] = [
  {
    kind: "race",
    plural: "races",
    label: "Policy races",
    icon: "check",
    question: "Which rule should we run?",
    render: (model) => <RaceView model={model as RaceViewModel} />,
  },
  {
    kind: "run",
    plural: "runs",
    label: "Runs",
    icon: "flow",
    question: "Will my orders finish on time?",
    render: null,
    coming: "The run viewer is on the way. Runs will show up here.",
  },
];

export function findDecision(page: string): {
  spec: DecisionKind;
  isList: boolean;
} | null {
  for (const spec of DECISION_VIEWS) {
    if (spec.plural === page) return { spec, isList: true };
    if (spec.kind === page) return { spec, isList: false };
  }
  return null;
}

const LEVEL: Record<string, { label: string; kind: "ok" | "risk" | "late" }> = {
  very_likely_better: { label: "Very likely better", kind: "ok" },
  maybe_better: { label: "Maybe better", kind: "risk" },
  same_policy: { label: "Same as today", kind: "risk" },
  unknown: { label: "Not sure yet", kind: "risk" },
  maybe_worse: { label: "Maybe worse", kind: "late" },
  very_likely_worse: { label: "Very likely worse", kind: "late" },
};

function when(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString();
}

export function DecisionList(props: {
  spec: DecisionKind;
  onOpen: (id: string) => void;
}) {
  const { spec, onOpen } = props;
  const rows = useQuery({
    queryKey: ["decision-list", spec.plural],
    queryFn: () => decisionApi.list(spec.plural),
    enabled: spec.render !== null,
  });
  return (
    <div className="tf-root">
      <Section
        id={spec.plural}
        eyebrow="Decisions"
        question={spec.question}
        hint={`Each row is a ${spec.label.toLowerCase().replace(/s$/, "")}. The sentence is the answer.`}
      >
        {spec.render === null ? (
          <EmptyState title="Coming soon." body={<p>{spec.coming}</p>} />
        ) : rows.isLoading ? (
          <p role="status">Loading.</p>
        ) : rows.error ? (
          <EmptyState
            title="Could not load this list."
            body={<p>{rows.error.message}</p>}
          />
        ) : rows.data?.length ? (
          <DecisionTable rows={rows.data} onOpen={onOpen} label={spec.label} />
        ) : (
          <EmptyState
            title="Nothing here yet."
            body={<p>Run one from the command line and it shows up here.</p>}
            fix="twinflow policies race <project> --out <folder>"
          />
        )}
      </Section>
    </div>
  );
}

function DecisionTable(props: {
  rows: DecisionRow[];
  onOpen: (id: string) => void;
  label: string;
}) {
  return (
    <div className="tf-card">
      <table className="tf-table ws-table-left" aria-label={props.label}>
        <thead>
          <tr>
            <th>Name</th>
            <th>Date</th>
            <th>Answer</th>
            <th>How sure</th>
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row) => {
            const level = LEVEL[row.level] ?? LEVEL.unknown;
            return (
              <tr key={row.id}>
                <td>
                  <a
                    href={`?page=race&id=${encodeURIComponent(row.id)}`}
                    onClick={(event) => {
                      event.preventDefault();
                      props.onOpen(row.id);
                    }}
                  >
                    {row.project || row.id}
                  </a>
                  <div className="tf-sub">{row.id}</div>
                </td>
                <td>{when(row.when)}</td>
                <td>{row.sentence}</td>
                <td>
                  <StatusPill kind={level.kind} label={level.label} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ExportButton({ kind, id }: { kind: string; id: string }) {
  const [state, setState] = useState<"idle" | "busy" | "failed">("idle");
  const save = async () => {
    setState("busy");
    try {
      download(
        `${kind}-${id}.html`,
        await decisionApi.exportHtml(kind, id),
        "text/html",
      );
      setState("idle");
    } catch {
      setState("failed");
    }
  };
  return (
    <>
      <button
        type="button"
        className="tf-btn"
        onClick={save}
        disabled={state === "busy"}
      >
        Export HTML
      </button>
      {state === "failed" && (
        <span role="alert">Export failed. Try again.</span>
      )}
    </>
  );
}

export function DecisionViewPage(props: {
  spec: DecisionKind;
  id: string;
  onBack: () => void;
}) {
  const { spec, id, onBack } = props;
  const model = useQuery({
    queryKey: ["decision-view", spec.kind, id],
    queryFn: () => decisionApi.view<unknown>(spec.kind, id),
    enabled: spec.render !== null && id !== "",
  });
  return (
    <ShellContext.Provider value="workspace">
      <PageActionsContext.Provider
        value={<ExportButton kind={spec.kind} id={id} />}
      >
        <button className="ws-back" onClick={onBack}>
          ← All {spec.label.toLowerCase()}
        </button>
        {spec.render === null ? (
          <div className="tf-root">
            <EmptyState title="Coming soon." body={<p>{spec.coming}</p>} />
          </div>
        ) : model.isLoading ? (
          <p role="status">Loading.</p>
        ) : model.error || !model.data ? (
          <div className="tf-root">
            <EmptyState
              title="Could not open this page."
              body={<p>{model.error?.message ?? "No data came back."}</p>}
            />
          </div>
        ) : (
          spec.render(model.data)
        )}
      </PageActionsContext.Provider>
    </ShellContext.Provider>
  );
}
