import { useEffect, useState } from "react";
import { api, pollJob, type Lever, type SweepResult } from "../api";
import { ConfidenceBand } from "./ConfidenceBand";
import { Spinner } from "./Spinner";

function parseValues(raw: string): number[] {
  return raw
    .split(",")
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.length > 0)
    .map(Number)
    .filter((value) => !Number.isNaN(value));
}

export function SweepTab({ model }: { model: string }) {
  const [levers, setLevers] = useState<Lever[]>([]);
  const [leverPath, setLeverPath] = useState<string>("");
  const [valuesText, setValuesText] = useState<string>("");
  const [reps, setReps] = useState(20);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<SweepResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLevers([]);
    setLeverPath("");
    setValuesText("");
    setResult(null);
    setError(null);
    api
      .levers(model)
      .then((list) => {
        setLevers(list);
        if (list.length > 0) {
          setLeverPath(list[0].path);
          setValuesText(defaultCandidates(list[0]));
        }
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [model]);

  function defaultCandidates(lever: Lever): string {
    const values = new Set<number>([lever.min, lever.current, lever.max]);
    return Array.from(values)
      .sort((a, b) => a - b)
      .join(", ");
  }

  function handleLeverChange(path: string) {
    setLeverPath(path);
    const lever = levers.find((item) => item.path === path);
    if (lever) setValuesText(defaultCandidates(lever));
  }

  async function handleSweep() {
    const values = parseValues(valuesText);
    if (!leverPath || values.length === 0) {
      setError("Enter at least one numeric value to sweep.");
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const job = await api.sweep({ model, sweep: { [leverPath]: values }, reps });
      const outcome = await pollJob<SweepResult>(job.id);
      setResult(outcome);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  const selectedLever = levers.find((item) => item.path === leverPath);

  return (
    <div>
      <div className="panel-header">
        <h2>Sweep</h2>
        <span className="hint">score a lever across several candidate values, side by side</span>
      </div>

      <div className="field-row card">
        <div className="field">
          <label htmlFor="sweep-lever">lever</label>
          <select
            id="sweep-lever"
            value={leverPath}
            onChange={(event) => handleLeverChange(event.target.value)}
            disabled={levers.length === 0}
          >
            {levers.map((lever) => (
              <option key={lever.path} value={lever.path}>
                {lever.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ minWidth: 240 }}>
          <label htmlFor="sweep-values">
            candidate values{selectedLever ? ` (range ${selectedLever.min}–${selectedLever.max})` : ""}
          </label>
          <input
            id="sweep-values"
            type="text"
            value={valuesText}
            onChange={(event) => setValuesText(event.target.value)}
            placeholder="e.g. 2, 3, 4"
          />
        </div>
        <div className="field">
          <label htmlFor="sweep-reps">replications</label>
          <input
            id="sweep-reps"
            type="number"
            min={1}
            max={200}
            value={reps}
            onChange={(event) => setReps(Number(event.target.value))}
          />
        </div>
        <button className="primary" onClick={handleSweep} disabled={running || !leverPath}>
          {running ? "Running…" : "Sweep"}
        </button>
      </div>

      {running && <Spinner label="Sweeping candidates…" />}
      {error && <div className="error-banner">{error}</div>}

      {result && (
        <table style={{ marginTop: 18 }}>
          <thead>
            <tr>
              <th>{selectedLever?.label ?? leverPath}</th>
              <th>on-time %</th>
              <th>run hours</th>
              <th>setup hours</th>
            </tr>
          </thead>
          <tbody>
            {result.points.map((point, index) => (
              <tr key={index}>
                <td className="mono">{String(point.levers[leverPath])}</td>
                <td>
                  <ConfidenceBand interval={point.intervals.on_time_pct} unit="%" compact />
                </td>
                <td className="mono">{point.kpis.run_hours.toFixed(1)}</td>
                <td className="mono">{point.kpis.setup_hours.toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!running && !result && !error && (
        <div className="empty-state">
          Pick a lever and candidate values to compare their outcomes.
        </div>
      )}
    </div>
  );
}
