import "./details.css";
import type { RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "about" | "baseline">;

/** Runs, days, seed and today's rule (DESIGN.md 3.11). */
export function RunFacts({ model }: { model: Source }) {
  const { about, baseline } = model;
  const facts: Array<[string, string]> = [
    ["Runs per rule", String(about.episodes)],
    ["Days per run", String(about.horizon_days)],
    ["Random seed", String(about.seed)],
    ["Today's rule", baseline?.label ?? "None"],
    ["Practice runs", String(about.tune_episodes)],
    ["Rules raced", String(about.policies_raced)],
  ];
  return (
    <dl className="tf-det-facts" aria-label="Run facts" data-testid="run-facts">
      {facts.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
