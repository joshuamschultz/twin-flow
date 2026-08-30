import { useState } from "react";
import { api, pollJob, type RunResult } from "../api";
import { ConfidenceBand } from "./ConfidenceBand";
import { Spinner } from "./Spinner";
import { UtilizationList } from "./UtilizationList";
import { FulfillmentCards } from "./FulfillmentCards";
import { InventoryTable } from "./InventoryTable";

export function RunTab({ model }: { model: string }) {
  const [reps, setReps] = useState(20);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleRun() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const job = await api.run({ model, reps });
      const outcome = await pollJob<RunResult>(job.id);
      setResult(outcome);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div>
      <div className="panel-header">
        <h2>Run</h2>
        <span className="hint">simulate the current plan across replications</span>
      </div>

      <div className="field-row card">
        <div className="field">
          <label htmlFor="run-reps">replications</label>
          <input
            id="run-reps"
            type="number"
            min={1}
            max={200}
            value={reps}
            onChange={(event) => setReps(Number(event.target.value))}
          />
        </div>
        <button className="primary" onClick={handleRun} disabled={running}>
          {running ? "Running…" : "Run"}
        </button>
      </div>

      {running && <Spinner label="Simulating…" />}
      {error && <div className="error-banner">{error}</div>}

      {result && (
        <div style={{ marginTop: 20, display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="card">
            <div className="card-title">On-Time Delivery</div>
            <div className="stat-value">
              {result.intervals.on_time_pct.mean.toFixed(1)}
              <span className="unit">%</span>
            </div>
            <ConfidenceBand interval={result.intervals.on_time_pct} unit="%" />
          </div>

          <div className="card-grid">
            <div className="card">
              <div className="card-title">Run Hours</div>
              <div className="stat-value">{result.kpis.run_hours.toFixed(1)}</div>
            </div>
            <div className="card">
              <div className="card-title">Setup Hours</div>
              <div className="stat-value">{result.kpis.setup_hours.toFixed(1)}</div>
            </div>
          </div>

          <div className="card">
            <div className="card-title">Utilization by Cell</div>
            <UtilizationList utilization={result.intervals.utilization_by_cell} />
          </div>

          <FulfillmentCards fulfillment={result.fulfillment} />

          {Object.keys(result.inventory.average_level).length > 0 && (
            <InventoryTable inventory={result.inventory} />
          )}
        </div>
      )}

      {!running && !result && !error && (
        <div className="empty-state">Run the plan to see on-time performance and utilization.</div>
      )}
    </div>
  );
}
