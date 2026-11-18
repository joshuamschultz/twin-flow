import "./details.css";
import { num, pct, signedUsd, usd } from "./format";
import { Card, Tag } from "./primitives";
import type { PolicyRow, RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "about" | "policies">;

const NONE = "–";
const SOURCE_WORDS: Record<string, string> = {
  project: "Your project",
  builtin: "Built in",
};

function opt(value: number | null, format: (v: number) => string): string {
  return value === null ? NONE : format(value);
}

function range(row: PolicyRow): string {
  const { low, high } = row.dollars;
  return low === null || high === null ? NONE : `${usd(low)} to ${usd(high)}`;
}

/** Every rule, one row each. This is the only place a "95% range" label appears. */
export function FullTable({ model }: { model: Source }) {
  const { about, policies } = model;
  const rangeName = `${Math.round(about.interval * 100)}% range`;
  const showScore = !about.score_is_dollars;
  const rows = [...policies].sort((a, b) => a.rank - b.rank);
  return (
    <Card
      question="Every rule, side by side"
      sub="All numbers are per run. Every rule faced the same customer orders."
    >
      <div
        className="tf-scroll"
        tabIndex={0}
        role="region"
        aria-label="All rules, full table"
      >
        <table className="tf-table tf-det-table" data-testid="full-table">
          <thead>
            <tr>
              <th scope="col">#</th>
              <th scope="col">Rule</th>
              <th scope="col">Profit per run</th>
              {showScore && <th scope="col">Score</th>}
              <th scope="col">{rangeName}</th>
              <th scope="col">vs today</th>
              <th scope="col">Runs won</th>
              <th scope="col">Filled</th>
              <th scope="col">Changeovers</th>
              <th scope="col">Colour changes</th>
              <th scope="col">Stock units</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.policy}>
                <td>{row.rank}</td>
                <th scope="row">
                  {row.label}
                  {row.is_recommended && <Tag kind="pick">pick</Tag>}
                  {row.is_baseline && <Tag kind="today">today</Tag>}
                </th>
                <td>
                  {usd(row.dollars.mean)}
                  {row.dollar_warning && (
                    <span className="tf-det-muted">{row.dollar_warning}</span>
                  )}
                </td>
                {showScore && <td>{num(row.score.mean, 2)}</td>}
                <td>{range(row)}</td>
                <td>
                  {row.is_baseline || !row.vs_baseline
                    ? "today"
                    : signedUsd(row.vs_baseline.mean)}
                </td>
                <td>
                  {row.is_baseline
                    ? ""
                    : `${row.wins_vs_baseline} of ${about.episodes}`}
                </td>
                <td>{opt(row.fill_rate, (v) => pct(v))}</td>
                <td>{opt(row.changeovers, (v) => num(v, 1))}</td>
                <td>{opt(row.cross_family, (v) => num(v, 1))}</td>
                <td>{opt(row.avg_stock, (v) => num(v))}</td>
                <td>{SOURCE_WORDS[row.source] ?? row.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
