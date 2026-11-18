import "./details.css";
import { plural, ruleLabel } from "./detailsFormat";
import type { Problem, Warning } from "./types";

/** Rules that could not run, and warnings about the race (DESIGN.md 3.12). */
export function ProblemList({
  problems,
  warnings,
  policies = [],
}: {
  problems: Problem[];
  warnings: Warning[];
  policies?: ReadonlyArray<{ policy: string; label: string }>;
}) {
  return (
    <div className="tf-card" data-testid="problem-list">
      <h3>Problems</h3>
      {problems.length === 0 ? (
        <p className="tf-sub">No problems were found in this race.</p>
      ) : (
        <>
          <p className="tf-sub">
            {problems.length} {plural(problems.length, "rule", "rules")} could
            not run. They are left out of the charts.
          </p>
          <ul className="tf-det-list">
            {problems.map((p, i) => (
              <li key={i} className="tf-det-problem">
                <div>
                  {p.policy && <b>{ruleLabel(p.policy, policies)}</b>}
                  {p.file && (
                    <span className="tf-det-where">
                      {p.policy ? " " : ""}
                      {p.file}
                      {p.line ? `:${p.line}` : ""}
                    </span>
                  )}
                </div>
                <div>{p.message}</div>
                <div>Fix: {p.fix}</div>
              </li>
            ))}
          </ul>
        </>
      )}
      {warnings.length > 0 && (
        <>
          <h3 className="tf-det-sub">Things to know</h3>
          <ul className="tf-det-list">
            {warnings.map((w, i) => (
              <li key={i} className="tf-det-problem">
                <div>{w.text}</div>
                <div>Fix: {w.fix}</div>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
