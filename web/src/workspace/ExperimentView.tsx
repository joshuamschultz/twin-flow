import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { download, workspaceApi, type Experiment, type Scenario } from "./api";
import { Icon } from "./Icons";
import { DomainResults } from "./DomainResults";

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
  return (
    <>
      <div className="ws-page-heading">
        <div>
          <span className="ws-eyebrow">EXPERIMENT LOG</span>
          <h1>Turn possibilities into evidence.</h1>
          <p>
            Every experiment is tied to a scenario, a snapshot, and its
            assumptions.
          </p>
        </div>
      </div>
      {!jobs.length ? (
        <div className="ws-empty">
          <Icon name="play" size={32} />
          <h2>Your first answer starts with a baseline.</h2>
          <p>
            Load a scenario, then run an experiment. Results and downloadable
            evidence will appear here.
          </p>
        </div>
      ) : (
        <div className="ws-experiment-layout">
          <aside className="ws-card ws-job-list">
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
                  <strong>
                    {scenarios.find(
                      (scenario) => scenario.id === item.scenario_id,
                    )?.name ?? "Scenario"}
                  </strong>
                  <small>
                    {item.reps} replications ·{" "}
                    {new Date(item.created_at).toLocaleTimeString([], {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </small>
                </div>
                <span className="ws-badge">
                  {item.status.replace(/_/g, " ")}
                </span>
              </button>
            ))}
          </aside>
          {job && (
            <section className="ws-card">
              <div className="ws-card-heading">
                <div>
                  <h2>Experiment {job.id.slice(0, 8)}</h2>
                  <p>
                    {job.reps} replications · seed {job.seed}
                  </p>
                </div>
                <button
                  className="ws-button"
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
              </div>
              {["queued", "running", "cancel_requested"].includes(
                job.status,
              ) && (
                <div className="ws-empty">
                  <span className="ws-running-ring" />
                  <h2>Testing this scenario…</h2>
                  <p>
                    Work continues on the server. You can explore another
                    scenario while it runs.
                  </p>
                  <button
                    className="ws-button"
                    disabled={job.status === "cancel_requested"}
                    onClick={() => onCancel(job.id)}
                  >
                    {job.status === "cancel_requested"
                      ? "Cancellation requested"
                      : "Cancel experiment"}
                  </button>
                </div>
              )}
              {job.error && (
                <div className="ws-error" role="alert">
                  {job.error}
                </div>
              )}
              {job.result && (
                <>
                  <div className="ws-result-metrics">
                    {Object.entries(job.result.metrics).map(([key, value]) => (
                      <div key={key}>
                        <span>{key.replace(/_/g, " ")}</span>
                        <strong>
                          {value.toFixed(2)}
                          {key.endsWith("pct") ? "%" : ""}
                        </strong>
                      </div>
                    ))}
                  </div>
                  <p className="ws-note">{job.result.interpretation}</p>
                  {job.result.domain && job.result.domain !== "manufacturing" ? <DomainResults result={job.result} /> : <>
                  <h3>Order completion outcomes</h3>
                  <p>
                    Simulation seconds from the scenario origin. Bands describe
                    outcome spread, not certainty about a promise.
                  </p>
                  <div className="ws-table-wrap">
                    <table>
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
                  </div>
                  </>}
                  <details className="ws-outcomes">
                    <summary>Replication outcomes and termination</summary>
                    <pre>{JSON.stringify(job.result.outcomes, null, 2)}</pre>
                  </details>
                  {complete.length > 1 && (
                    <div className="ws-comparison">
                      <h3>Compare with a baseline</h3>
                      <p>
                        Use matching demand, seed, and replication settings.
                      </p>
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
                                {
                                  scenarios.find(
                                    (s) => s.id === item.scenario_id,
                                  )?.name
                                }{" "}
                                · {item.id.slice(0, 6)}
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
                        <>
                          <div className="ws-result-metrics">
                            {Object.entries(comparison.data.delta).map(
                              ([key, value]) => (
                                <div key={key}>
                                  <span>Δ {key.replace(/_/g, " ")}</span>
                                  <strong>
                                    {value > 0 ? "+" : ""}
                                    {value.toFixed(2)}
                                  </strong>
                                </div>
                              ),
                            )}
                          </div>
                          <p>{comparison.data.interpretation}</p>
                        </>
                      )}
                    </div>
                  )}
                </>
              )}
            </section>
          )}
        </div>
      )}
    </>
  );
}
