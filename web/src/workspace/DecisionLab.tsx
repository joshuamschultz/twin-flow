import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { download, workspaceApi } from "./api";
import { AnswerHero } from "../decision/AnswerHero";
import { Card, Section, StatusPill } from "../decision/primitives";
import { Icon } from "./Icons";
import { ResultPage } from "./ResultPage";

export function DecisionLab({ mode }: { mode: "data" | "schedule" }) {
  const [content, setContent] = useState("");
  const [backend, setBackend] = useState("baseline");
  const [knownAt, setKnownAt] = useState("2026-01-10T00:00:00Z");
  const [fileError, setFileError] = useState("");
  const contracts = useQuery({
    queryKey: ["tool-contracts"],
    queryFn: workspaceApi.contracts,
  });
  const run = useMutation({
    mutationFn: async () => {
      if (mode === "schedule")
        return workspaceApi.schedule(JSON.parse(content), backend);
      const text = content.trim();
      const parsed = text.startsWith("[")
        ? JSON.parse(text)
        : text
            .split("\n")
            .filter(Boolean)
            .map((line) => JSON.parse(line));
      return workspaceApi.ingest(parsed);
    },
  });
  const snapshot = useMutation({
    mutationFn: () => workspaceApi.snapshot(knownAt),
  });
  const result = mode === "data" ? (snapshot.data ?? run.data) : run.data;
  const schedule = run.data?.result as
    | {
        verified: boolean;
        schedule: {
          status: string;
          assignments: {
            operation_id: string;
            machine_id: string;
            start: number;
            end: number;
          }[];
        };
      }
    | undefined;
  const assignments = schedule?.schedule.assignments ?? [];
  const horizon = Math.max(1, ...assignments.map((item) => item.end));
  const verified = schedule?.verified;
  const eyebrow =
    mode === "data"
      ? "CONNECT FACTS TO THE MODEL"
      : "TURN CONSTRAINTS INTO A PLAN";
  const headline =
    mode === "schedule" && schedule
      ? verified
        ? "A verified schedule is ready."
        : "No verified schedule yet."
      : mode === "data"
        ? "A clear view of what is known."
        : "Find a schedule that fits.";
  const lede =
    mode === "data"
      ? "Retain source events. Reconcile corrections. Build an inspectable snapshot for your agent."
      : "Model precedence, readiness, qualified resources, and frozen work. Every candidate is checked independently.";
  return (
    <ResultPage
      view={mode === "data" ? "data" : "schedule"}
      title={mode === "data" ? "Operational data" : "Scheduling"}
      project="Your workspace"
      scope={mode === "data" ? "Source events and snapshots" : "Checked plans"}
      sections={[
        { id: "decision", label: "Answer" },
        { id: "input", label: mode === "data" ? "Events" : "Problem" },
        { id: "result", label: "Result" },
      ]}
      hero={<AnswerHero eyebrow={eyebrow} headline={headline} lede={lede} />}
    >
      <Section
        id="input"
        eyebrow={mode === "data" ? "Events" : "Problem"}
        question={
          mode === "data"
            ? "What facts do we have?"
            : "What must the schedule respect?"
        }
      >
        <div className="ws-two-column">
          <Card
            question={
              mode === "data"
                ? "Import operational events"
                : "Scheduling problem"
            }
          >
            <div className="ws-actions">
              <button
                className="ws-button"
                disabled={!contracts.data}
                onClick={() => {
                  setContent(
                    JSON.stringify(
                      mode === "data"
                        ? contracts.data!.event_example
                        : contracts.data!.scheduling_example,
                      null,
                      2,
                    ),
                  );
                  run.reset();
                  snapshot.reset();
                }}
              >
                Load example
              </button>
            </div>
            <label className="ws-field">
              Import {mode === "data" ? "JSON / JSONL" : "JSON"}
              <input
                type="file"
                accept=".json,.jsonl"
                onChange={async (event) => {
                  const file = event.target.files?.[0];
                  if (!file) return;
                  if (file.size > 5_242_880) {
                    setFileError("Choose a file smaller than 5 MB.");
                    return;
                  }
                  try {
                    setContent(await file.text());
                    setFileError("");
                    run.reset();
                    snapshot.reset();
                  } catch {
                    setFileError("The file could not be read.");
                  }
                }}
              />
            </label>
            <label className="ws-field">
              {mode === "data"
                ? "Normalized source records"
                : "Operations and resource windows"}
              <textarea
                className="ws-json-editor"
                rows={17}
                value={content}
                onChange={(event) => setContent(event.target.value)}
                spellCheck={false}
              />
            </label>
            {mode === "schedule" && (
              <label className="ws-field">
                Scheduling method
                <select
                  value={backend}
                  onChange={(event) => setBackend(event.target.value)}
                >
                  <option value="baseline">Earliest-feasible baseline</option>
                  <option value="ortools">CP-SAT optimization</option>
                </select>
              </label>
            )}
            {(fileError || run.error || contracts.error) && (
              <p className="ws-error" role="alert">
                {fileError || run.error?.message || contracts.error?.message}
              </p>
            )}
            <button
              className="ws-button primary"
              disabled={!content || run.isPending}
              onClick={() => run.mutate()}
            >
              <Icon name="play" size={16} />
              {run.isPending
                ? "Working…"
                : mode === "data"
                  ? "Import events"
                  : "Find schedule"}
            </button>
            {mode === "data" && run.data && (
              <p role="status">
                {String(run.data.inserted)} events inserted ·{" "}
                {String(run.data.duplicates)} duplicates retained once.
              </p>
            )}
          </Card>
          {mode === "data" ? (
            <Card
              question="Build a historical snapshot"
              sub="Only facts known by the cutoff participate. Export the snapshot so your agent can map entities into a scenario."
            >
              <label className="ws-field">
                Known at · ISO timestamp
                <input
                  value={knownAt}
                  onChange={(event) => setKnownAt(event.target.value)}
                />
              </label>
              <button
                className="ws-button"
                disabled={snapshot.isPending}
                onClick={() => snapshot.mutate()}
              >
                Reconcile snapshot
              </button>
              {snapshot.error && (
                <p role="alert" className="ws-error">
                  {snapshot.error.message}
                </p>
              )}
            </Card>
          ) : (
            <Card
              question="Feasibility before improvement."
              sub="An optimization timeout is not proof of infeasibility. Review status, verification, objective, and bounds in the evidence."
            />
          )}
        </div>
      </Section>
      <Section
        id="result"
        eyebrow="Result"
        question={
          mode === "data" ? "What did we keep?" : "Does the plan hold up?"
        }
      >
        {!result && !schedule && (
          <Card
            question={
              mode === "data"
                ? "Keep the source attached."
                : "Nothing to check yet."
            }
            sub={
              mode === "data"
                ? "Stable source IDs and revisions keep retries idempotent. Missing and stale facts stay visible instead of becoming confident guesses."
                : "Find a schedule and its checks show up here."
            }
          />
        )}
        <div className="ws-stack">
          {schedule && (
            <Card question="Schedule result">
              <StatusPill
                kind={schedule.verified ? "ok" : "risk"}
                label={`${schedule.schedule.status} · ${schedule.verified ? "Verified" : "No verified plan"}`}
              />
              <div className="ws-schedule-bars">
                {assignments.map((item) => (
                  <div key={item.operation_id}>
                    <strong>
                      {item.operation_id} <small>{item.machine_id}</small>
                    </strong>
                    <div className="ws-schedule-track">
                      <span
                        style={{
                          marginLeft: `${(100 * item.start) / horizon}%`,
                          width: `${(100 * (item.end - item.start)) / horizon}%`,
                        }}
                      />
                    </div>
                    <small>
                      {item.start} → {item.end}
                    </small>
                  </div>
                ))}
              </div>
              {schedule.verified && (
                <button
                  className="ws-button"
                  onClick={() =>
                    download(
                      "schedule.csv",
                      "operation,resource,start,end\n" +
                        assignments
                          .map((item) =>
                            [
                              item.operation_id,
                              item.machine_id,
                              item.start,
                              item.end,
                            ]
                              .map(
                                (value) =>
                                  `"${String(value).replace(/"/g, '""')}"`,
                              )
                              .join(","),
                          )
                          .join("\n"),
                      "text/csv",
                    )
                  }
                >
                  Export schedule CSV
                </button>
              )}
            </Card>
          )}
          {result && (
            <Card question="Retained evidence">
              <div className="ws-actions">
                <button
                  className="ws-button"
                  onClick={() =>
                    download(
                      `${mode}-evidence.json`,
                      JSON.stringify(result, null, 2),
                    )
                  }
                >
                  <Icon name="down" size={16} />
                  Export JSON
                </button>
              </div>
              <pre className="ws-source">{JSON.stringify(result, null, 2)}</pre>
            </Card>
          )}
        </div>
      </Section>
    </ResultPage>
  );
}
