import "./details.css";
import { fmtMoney, fmtNum, fmtPct } from "./detailsFormat";
import type { PolicyRow, RaceViewModel } from "./types";

type Source = Pick<RaceViewModel, "about" | "policies">;

const SOURCE_WORDS: Record<string, string> = {
  project: "Your project",
  builtin: "Built in",
};

function range(row: PolicyRow): string {
  const { low, high } = row.dollars;
  return low === null || high === null
    ? "–"
    : `${fmtMoney(low)} to ${fmtMoney(high)}`;
}

/** Every rule, one row each. This is the only place a "95% range" label appears. */
export function FullTable({ model }: { model: Source }) {
  const { about, policies } = model;
  const rangeName = `${Math.round(about.interval * 100)}% range`;
  const showScore = !about.score_is_dollars;
  const rows = [...policies].sort((a, b) => a.rank - b.rank);
  return (
    <div className="tf-card">
      <h3>Every rule, side by side</h3>
      <p className="tf-sub">
        All numbers are per run. Every rule faced the same customer orders.
      </p>
      <div
        className="tf-det-scroll"
        tabIndex={0}
        role="region"
        aria-label="All rules, full table"
      >
        <table className="tf-det-table" data-testid="full-table">
          <thead>
            <tr>
              <th scope="col" className="n">
                #
              </th>
              <th scope="col">Rule</th>
              <th scope="col" className="n">
                Profit per run
              </th>
              {showScore && (
                <th scope="col" className="n">
                  Score
                </th>
              )}
              <th scope="col" className="n">
                {rangeName}
              </th>
              <th scope="col" className="n">
                vs today
              </th>
              <th scope="col" className="n">
                Runs won
              </th>
              <th scope="col" className="n">
                Filled
              </th>
              <th scope="col" className="n">
                Changeovers
              </th>
              <th scope="col" className="n">
                Colour changes
              </th>
              <th scope="col" className="n">
                Stock units
              </th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.policy}>
                <td className="n">{row.rank}</td>
                <th scope="row">
                  {row.label}
                  {row.is_recommended && (
                    <span className="tf-tag tf-tag-pick">pick</span>
                  )}
                  {row.is_baseline && <span className="tf-tag">today</span>}
                </th>
                <td className="n">
                  {fmtMoney(row.dollars.mean)}
                  {row.dollar_warning && (
                    <span className="tf-det-muted">{row.dollar_warning}</span>
                  )}
                </td>
                {showScore && (
                  <td className="n">{fmtNum(row.score.mean, 2)}</td>
                )}
                <td className="n">{range(row)}</td>
                <td className="n">
                  {row.is_baseline || !row.vs_baseline
                    ? "today"
                    : fmtMoney(row.vs_baseline.mean, { signed: true })}
                </td>
                <td className="n">
                  {row.is_baseline
                    ? ""
                    : `${row.wins_vs_baseline} of ${about.episodes}`}
                </td>
                <td className="n">{fmtPct(row.fill_rate)}</td>
                <td className="n">{fmtNum(row.changeovers, 1)}</td>
                <td className="n">{fmtNum(row.cross_family, 1)}</td>
                <td className="n">{fmtNum(row.avg_stock, 0)}</td>
                <td>{SOURCE_WORDS[row.source] ?? row.source}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
