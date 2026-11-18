// DESIGN.md 2.2: the DECISIONS pages of the workspace. A list per view, and one page per
// view that mounts the shared view component with shell="workspace".
// A new view plugs in with one line in DECISION_VIEWS.
import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { PageActionsContext } from "../decision/Page";
import { EmptyState, Section, StatusPill } from "../decision/primitives";
import { ImproveView } from "../decision/improve/ImproveView";
import { ProjectView } from "../decision/project/ProjectView";
import { StatesView } from "../decision/states/StatesView";
import type {
  ImproveViewModel,
  ProjectViewModel,
  RaceViewModel,
  RunViewModel,
  StatesViewModel,
  SweepViewModel,
} from "../decision/types";
import { ShellContext } from "../decision/viewState";
import { RaceView } from "../decision/views/RaceView";
import { RunView } from "../decision/views/RunView";
import { SweepView } from "../decision/views/SweepView";
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
  /** the view, drawn from its model */
  render: (model: unknown) => ReactNode;
  /** the command that makes one, for the empty list */
  make: string;
};

export const DECISION_VIEWS: DecisionKind[] = [
  {
    kind: "race",
    plural: "races",
    label: "Policy races",
    icon: "check",
    question: "Which rule should we run?",
    render: (model) => <RaceView model={model as RaceViewModel} />,
    make: "twinflow policies race <project> --workspace <folder>",
  },
  {
    kind: "run",
    plural: "runs",
    label: "Runs",
    icon: "flow",
    question: "Will my orders finish on time?",
    render: (model) => <RunView model={model as RunViewModel} />,
    make: "TWINFLOW_WORKSPACE_ROOT=<folder> twinflow run <model> --plan <plan>",
  },
  {
    kind: "sweep",
    plural: "sweeps",
    label: "Sweeps",
    icon: "branch",
    question: "What one change would help most?",
    render: (model) => <SweepView model={model as SweepViewModel} />,
    make: "TWINFLOW_WORKSPACE_ROOT=<folder> twinflow balance <model> --sweep <file>",
  },
  {
    kind: "states",
    plural: "states",
    label: "States",
    icon: "grid",
    question: "What situations does the plant live in?",
    render: (model) => <StatesView model={model as StatesViewModel} />,
    make: "twinflow states <project> --html",
  },
  {
    kind: "improve",
    plural: "improves",
    label: "Improve",
    icon: "spark",
    question: "Did the new rule win?",
    render: (model) => <ImproveView model={model as ImproveViewModel} />,
    make: "twinflow improve <project> <candidate>",
  },
  {
    kind: "project",
    plural: "projects",
    label: "Projects",
    icon: "box",
    question: "Is the project ready to run?",
    render: (model) => <ProjectView model={model as ProjectViewModel} />,
    make: "twinflow extensions check <project>",
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

const LEVEL: Record<
  string,
  { label: string; kind: "ok" | "risk" | "late" } | undefined
> = {
  very_likely_better: { label: "Very likely better", kind: "ok" },
  maybe_better: { label: "Maybe better", kind: "risk" },
  same_policy: { label: "Same as today", kind: "risk" },
  unknown: { label: "Not sure yet", kind: "risk" },
  maybe_worse: { label: "Maybe worse", kind: "late" },
  very_likely_worse: { label: "Very likely worse", kind: "late" },
  // run viewer
  all_on_time: { label: "All on time", kind: "ok" },
  most_on_time: { label: "Most on time", kind: "risk" },
  many_late: { label: "Many late", kind: "late" },
  // improve
  won: { label: "Won", kind: "ok" },
  ahead_not_proven: { label: "Ahead, not proven", kind: "risk" },
  lost: { label: "Lost", kind: "late" },
  // project
  ready: { label: "Ready", kind: "ok" },
  needs_fixes: { label: "Needs fixes", kind: "risk" },
  broken: { label: "Broken", kind: "late" },
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
  });
  return (
    <div className="tf-root">
      <Section
        id={spec.plural}
        eyebrow="Decisions"
        question={spec.question}
        hint={`Each row is a ${spec.label.toLowerCase().replace(/s$/, "")}. The sentence is the answer.`}
      >
        {rows.isLoading ? (
          <p role="status">Loading.</p>
        ) : rows.error ? (
          <EmptyState
            title="Could not load this list."
            body={<p>{rows.error.message}</p>}
          />
        ) : rows.data?.length ? (
          <DecisionTable
            rows={rows.data}
            onOpen={onOpen}
            label={spec.label}
            kind={spec.kind}
          />
        ) : (
          <EmptyState
            title="Nothing here yet."
            body={<p>Run one from the command line and it shows up here.</p>}
            fix={spec.make}
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
  kind: string;
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
            // Views with no "how sure" (states, sweeps) leave the cell empty.
            const level =
              LEVEL[row.level] ??
              (props.kind === "race" ? LEVEL.unknown : undefined);
            return (
              <tr key={row.id}>
                <td>
                  <a
                    href={`?page=${props.kind}&id=${encodeURIComponent(row.id)}`}
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
                  {level && (
                    <StatusPill kind={level.kind} label={level.label} />
                  )}
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
    enabled: id !== "",
  });
  return (
    <ShellContext.Provider value="workspace">
      <PageActionsContext.Provider
        value={<ExportButton kind={spec.kind} id={id} />}
      >
        <button className="ws-back" onClick={onBack}>
          ← All {spec.label.toLowerCase()}
        </button>
        {model.isLoading ? (
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
