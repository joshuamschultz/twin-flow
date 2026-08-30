import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { api, pollJob, type Lever, type ModulesResponse, type OptimizeResult } from "../api";
import { ConfidenceBand } from "./ConfidenceBand";
import { Spinner } from "./Spinner";

interface LeverSelection {
  enabled: boolean;
  min: number;
  max: number;
}

export function OptimizeTab({ model, modules }: { model: string; modules: ModulesResponse }) {
  const [levers, setLevers] = useState<Lever[]>([]);
  const [selection, setSelection] = useState<Record<string, LeverSelection>>({});
  const [objective, setObjective] = useState(modules.objectives[0] ?? "on_time_pct");
  const [optimizer, setOptimizer] = useState(modules.optimizers[0] ?? "hill_climb");
  const [budget, setBudget] = useState(12);
  const [reps, setReps] = useState(12);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<OptimizeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLevers([]);
    setSelection({});
    setResult(null);
    setError(null);
    api
      .levers(model)
      .then((list) => {
        setLevers(list);
        const initial: Record<string, LeverSelection> = {};
        list.forEach((lever, index) => {
          initial[lever.path] = { enabled: index === 0, min: lever.min, max: lever.max };
        });
        setSelection(initial);
      })
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, [model]);

  function toggleLever(path: string) {
    setSelection((prev) => ({
      ...prev,
      [path]: { ...prev[path], enabled: !prev[path].enabled },
    }));
  }

  function updateBound(path: string, bound: "min" | "max", value: number) {
    setSelection((prev) => ({
      ...prev,
      [path]: { ...prev[path], [bound]: value },
    }));
  }

  const activeCount = Object.values(selection).filter((item) => item.enabled).length;

  async function handleOptimize() {
    const space: Record<string, { min: number; max: number }> = {};
    for (const [path, sel] of Object.entries(selection)) {
      if (sel.enabled) space[path] = { min: sel.min, max: sel.max };
    }
    if (Object.keys(space).length === 0) {
      setError("Select at least one lever to search.");
      return;
    }
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const job = await api.optimize({
        model,
        space,
        objective,
        optimizer,
        budget,
        reps,
      });
      const outcome = await pollJob<OptimizeResult>(job.id);
      setResult(outcome);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  const chartData = useMemo(() => {
    if (!result) return [];
    let runningBest = result.direction === "max" ? -Infinity : Infinity;
    return result.history.map((entry, index) => {
      runningBest =
        result.direction === "max"
          ? Math.max(runningBest, entry.score)
          : Math.min(runningBest, entry.score);
      return { evaluation: index + 1, score: entry.score, best: runningBest };
    });
  }, [result]);

  return (
    <div>
      <div className="panel-header">
        <h2>Optimize</h2>
        <span className="hint">search a lever space under an objective</span>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="field-row">
          <div className="field">
            <label htmlFor="opt-objective">objective</label>
            <select
              id="opt-objective"
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
            >
              {modules.objectives.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-optimizer">optimizer</label>
            <select
              id="opt-optimizer"
              value={optimizer}
              onChange={(event) => setOptimizer(event.target.value)}
            >
              {modules.optimizers.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="opt-budget">budget</label>
            <input
              id="opt-budget"
              type="number"
              min={1}
              max={500}
              value={budget}
              onChange={(event) => setBudget(Number(event.target.value))}
            />
          </div>
          <div className="field">
            <label htmlFor="opt-reps">replications</label>
            <input
              id="opt-reps"
              type="number"
              min={1}
              max={200}
              value={reps}
              onChange={(event) => setReps(Number(event.target.value))}
            />
          </div>
          <button className="primary" onClick={handleOptimize} disabled={running || activeCount === 0}>
            {running ? "Searching…" : "Optimize"}
          </button>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="card-title">Levers to search</div>
        <div className="lever-picker">
          {levers.map((lever) => {
            const sel = selection[lever.path];
            if (!sel) return null;
            return (
              <div className="lever-picker-row" key={lever.path}>
                <input
                  type="checkbox"
                  checked={sel.enabled}
                  onChange={() => toggleLever(lever.path)}
                />
                <span>{lever.label}</span>
                <input
                  type="number"
                  value={sel.min}
                  onChange={(event) => updateBound(lever.path, "min", Number(event.target.value))}
                  disabled={!sel.enabled}
                />
                <input
                  type="number"
                  value={sel.max}
                  onChange={(event) => updateBound(lever.path, "max", Number(event.target.value))}
                  disabled={!sel.enabled}
                />
              </div>
            );
          })}
          {levers.length === 0 && <div className="empty-state">No tunable levers for this model.</div>}
        </div>
      </div>

      {running && <Spinner label="Searching the lever space…" />}
      {error && <div className="error-banner">{error}</div>}

      {result && (
        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <div className="card-grid">
            <div className="card">
              <div className="card-title">Best {result.objective} ({result.direction})</div>
              <div className="stat-value">{result.best_score.toFixed(3)}</div>
              <div className="footnote">{result.evaluations_used} evaluations used</div>
            </div>
            <div className="card">
              <div className="card-title">Winning Scenario</div>
              {Object.entries(result.best.levers).map(([path, value]) => (
                <div key={path} className="mono" style={{ fontSize: 12, marginBottom: 3 }}>
                  {path} = {String(value)}
                </div>
              ))}
            </div>
          </div>

          <div className="card">
            <div className="card-title">Winning Scenario — On-Time Delivery</div>
            <div className="stat-value">
              {result.best.intervals.on_time_pct.mean.toFixed(1)}
              <span className="unit">%</span>
            </div>
            <ConfidenceBand interval={result.best.intervals.on_time_pct} unit="%" />
          </div>

          <div className="card">
            <div className="card-title">Convergence</div>
            <ResponsiveContainer width="100%" height={260}>
              <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
                <XAxis
                  dataKey="evaluation"
                  stroke="var(--text-faint)"
                  tick={{ fill: "var(--text-faint)", fontSize: 11 }}
                  label={{ value: "evaluation", position: "insideBottom", offset: -4, fill: "var(--text-faint)", fontSize: 11 }}
                />
                <YAxis
                  stroke="var(--text-faint)"
                  tick={{ fill: "var(--text-faint)", fontSize: 11 }}
                />
                <Tooltip
                  contentStyle={{
                    background: "var(--surface)",
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    fontSize: 12,
                  }}
                />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line
                  type="monotone"
                  dataKey="score"
                  name="evaluated score"
                  stroke="var(--text-faint)"
                  strokeWidth={1.5}
                  dot={{ r: 2 }}
                />
                <Line
                  type="stepAfter"
                  dataKey="best"
                  name="running best"
                  stroke="var(--arc)"
                  strokeWidth={2}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
            <div className="footnote">the twin scores; you decide.</div>
          </div>
        </div>
      )}

      {!running && !result && !error && (
        <div className="empty-state">Choose levers and search for the highest-scoring scenario.</div>
      )}
    </div>
  );
}
