import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { download, workspaceApi, type Experiment, type Scenario } from "./api";
import { AnswerHero } from "../decision/AnswerHero";
import { Card, EmptyState, Section, StatusPill } from "../decision/primitives";
import { Icon } from "./Icons";
import { DomainResults } from "./DomainResults";
import { ResultPage } from "./ResultPage";

export function ExperimentView({
  jobs,
  scenarios,
  onCancel,
}: {
  jobs: Experiment[];
  scenarios: Scenario[];
  onCancel: (id: string) => void;
}) {
  const [selectedId, setSelectedId] = useState("");
  const [baseline, setBaseline] = useState("");
  const job = jobs.find((item) => item.id === selectedId) ?? jobs[0];
  const comparison = useMutation({
    mutationFn: () => workspaceApi.compare(baseline, job!.id),
  });
  const complete = jobs.filter((item) => item.status === "completed");
  const scenarioName = (item: Experiment) =>
    scenarios.find((scenario) => scenario.id === item.scenario_id)?.name ??
    "Scenario";
  if (!jobs.length)
    return (
      <div className="tf-root">
        <Section
          id="decision"
          eyebrow="Experiments"
          question="What did we learn?"
        >
          <EmptyState
            title="Your first answer starts with a baseline."
            body={
              <p>
                Load a scenario, then run an experiment. Results and
                downloadable evidence will appear here.
              </p>
            }
          />
        </Section>
      </div>
    );
  return (
    <div className="ws-experiment-layout">
      <aside className="tf-card ws-job-list">
        {jobs.map((item) => (
          <button
            key={item.id}
            className={job?.id === item.id ? "active" : ""}
            onClick={() => {
              setSelectedId(item.id);
              comparison.reset();
            }}
          >
            <span className={`ws-status-dot ${item.status}`} />
            <div>
              <strong>{scenarioName(item)}</strong>
              <small>
                {item.reps} replications ·{" "}
                {new Date(item.created_at).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </small>
            </div>
            <StatusPill
              kind={statusKind(item.status)}
              label={item.status.replace(/_/g, " ")}
            />
          </button>
        ))}
      </aside>
      {job && (
        <ResultPage
          view="experiment"
          title="Experiment"
          project={scenarioName(job)}
          scope={`${job.reps} replications · seed ${job.seed}`}
          sections={
            job.result
              ? [
                  { id: "decision", label: "Answer" },
                  { id: "outcomes", label: "Outcomes" },
                  ...(complete.length > 1
                    ? [{ id: "compare", label: "Compare" }]
                    : []),
                  { id: "details", label: "Details" },
                ]
              : [{ id: "decision", label: "Answer" }]
          }
          actions={
            <button
              type="button"
              className="tf-btn"
              onClick={() =>
                download(
                  `experiment-${job.id.slice(0, 8)}.json`,
                  JSON.stringify(job, null, 2),
                )
              }
            >
              <Icon name="down" size={16} />
              Evidence
            </button>
          }
          hero={<ExperimentHero job={job} onCancel={onCancel} />}
        >
          {job.error && (
            <div className="ws-error" role="alert">
              {job.error}
            </div>
          )}
          {job.result && (
            <>
              <Section
                id="outcomes"
                eyebrow="Outcomes"
                question="When does each order finish?"
              >
                {job.result.domain && job.result.domain !== "manufacturing" ? (
                  <DomainResults result={job.result} />
                ) : (
                  <Card
                    question="Order completion outcomes"
                    sub="Simulation seconds from the scenario origin. Bands describe outcome spread, not certainty about a promise."
                  >
                    <table className="tf-table ws-table-left">
                      <thead>
                        <tr>
                          <th>Order</th>
                          <th>Median</th>
                          <th>P10</th>
                          <th>P90</th>
                          <th>Observed</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(
                          job.result.intervals?.completion_distributions ?? {},
                        ).map(([id, band]) => (
                          <tr key={id}>
                            <td>{id}</td>
                            <td>
                              {band.quantiles
                                ? band.quantiles.p50.toFixed(1)
                                : "Not estimable"}
                            </td>
                            <td>
                              {band.quantiles
                                ? band.quantiles.p10.toFixed(1)
                                : "—"}
                            </td>
                            <td>
                              {band.quantiles
                                ? band.quantiles.p90.toFixed(1)
                                : "—"}
                            </td>
                            <td>
                              {band.observed_count} / {job.reps}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </Card>
                )}
              </Section>
              {complete.length > 1 && (
                <Section
                  id="compare"
                  eyebrow="Compare"
                  question="Is this better than another run?"
                  hint="Use matching demand, seed, and replication settings."
                >
                  <Card
                    question="Compare with a baseline"
                    takeaway={comparison.data?.interpretation}
                  >
                    <div className="ws-actions">
                      <select
                        aria-label="Baseline experiment"
                        value={baseline}
                        onChange={(event) => {
                          setBaseline(event.target.value);
                          comparison.reset();
                        }}
                      >
                        <option value="">Choose completed experiment</option>
                        {complete
                          .filter((item) => item.id !== job.id)
                          .map((item) => (
                            <option key={item.id} value={item.id}>
                              {scenarioName(item)} · {item.id.slice(0, 6)}
                            </option>
                          ))}
                      </select>
                      <button
                        className="ws-button"
                        disabled={!baseline || comparison.isPending}
                        onClick={() => comparison.mutate()}
                      >
                        Compare
                        <Icon name="arrow" size={16} />
                      </button>
                    </div>
                    {comparison.error && (
                      <p className="ws-error">{comparison.error.message}</p>
                    )}
                    {comparison.data && (
                      <Metrics
                        items={Object.entries(comparison.data.delta).map(
                          ([key, value]) => ({
                            label: `Δ ${key.replace(/_/g, " ")}`,
                            value: `${value > 0 ? "+" : ""}${value.toFixed(2)}`,
                          }),
                        )}
                      />
                    )}
                  </Card>
                </Section>
              )}
              <Section
                id="details"
                eyebrow="Details"
                question="What happened in every run?"
              >
                <Card question="Every number from this experiment">
                  <Metrics
                    items={Object.entries(job.result.metrics).map(
                      ([key, value]) => ({
                        label: key.replace(/_/g, " "),
                        value: `${value.toFixed(2)}${key.endsWith("pct") ? "%" : ""}`,
                      }),
                    )}
                  />
                  <details className="ws-outcomes">
                    <summary>Replication outcomes and termination</summary>
                    <pre>{JSON.stringify(job.result.outcomes, null, 2)}</pre>
                  </details>
                </Card>
              </Section>
            </>
          )}
        </ResultPage>
      )}
    </div>
  );
}

function statusKind(status: string): "ok" | "risk" | "late" {
  if (status === "completed") return "ok";
  return ["failed", "interrupted", "canceled"].includes(status)
    ? "late"
    : "risk";
}

function Metrics({ items }: { items: { label: string; value: string }[] }) {
  return (
    <div className="ws-result-metrics">
      {items.map((item) => (
        <div key={item.label}>
          <span>{item.label}</span>
          <strong>{item.value}</strong>
        </div>
      ))}
    </div>
  );
}

function ExperimentHero({
  job,
  onCancel,
}: {
  job: Experiment;
  onCancel: (id: string) => void;
}) {
  const eyebrow = `Experiment ${job.id.slice(0, 8)} · what did the test runs show?`;
  if (["queued", "running", "cancel_requested"].includes(job.status))
    return (
      <AnswerHero
        eyebrow={eyebrow}
        headline="Testing this scenario."
        lede="Work continues on the server. You can explore another scenario while it runs."
        actions={
          <button
            className="tf-btn"
            disabled={job.status === "cancel_requested"}
            onClick={() => onCancel(job.id)}
          >
            {job.status === "cancel_requested"
              ? "Cancellation requested"
              : "Cancel experiment"}
          </button>
        }
      />
    );
  if (!job.result)
    return (
      <AnswerHero
        eyebrow={eyebrow}
        headline="This experiment has no result."
        lede="See the error below, then run it again."
      />
    );
  return (
    <AnswerHero
      eyebrow={eyebrow}
      headline={job.result.interpretation}
      lede={`${job.reps} test runs with seed ${job.seed}. Every run uses the same demand.`}
      figures={Object.entries(job.result.metrics).map(([key, value]) => ({
        value: `${value.toFixed(2)}${key.endsWith("pct") ? "%" : ""}`,
        label: key.replace(/_/g, " "),
      }))}
    />
  );
}
