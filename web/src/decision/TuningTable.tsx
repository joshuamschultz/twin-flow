import "./details.css";
import { fmtMoney } from "./detailsFormat";
import type { TuningRow } from "./tuningModel";

/** One row per rule: settings tried, best setting, best to worst, steady or touchy.
 * A touchy rule needs care before a plant relies on it. */
export function TuningTable({
  rows,
  selected,
  onSelect,
}: {
  rows: TuningRow[];
  selected?: string;
  onSelect?: (policy: string) => void;
}) {
  return (
    <div
      className="tf-tune-scroll"
      tabIndex={0}
      role="region"
      aria-label="How touchy each rule is"
    >
      <table className="tf-tune-table" data-testid="tuning-table">
        <thead>
          <tr>
            <th scope="col">Rule</th>
            <th scope="col" className="n">
              Tried
            </th>
            <th scope="col">Best setting</th>
            <th scope="col" className="n">
              Best to worst
            </th>
            <th scope="col">
              <span className="tf-sr">Steady or touchy</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.policy}
              className={row.policy === selected ? "is-selected" : undefined}
            >
              <th scope="row" className="tf-tune-rule">
                {onSelect ? (
                  <button
                    type="button"
                    className="tf-link-btn"
                    aria-pressed={row.policy === selected}
                    onClick={() => onSelect(row.policy)}
                  >
                    {row.label}
                  </button>
                ) : (
                  row.label
                )}
                {row.isPick && <span className="tf-tag tf-tag-pick">pick</span>}
              </th>
              <td className="n">{row.tried}</td>
              <td>{row.best}</td>
              <td className="n">{fmtMoney(row.spread)}</td>
              <td>
                <span
                  className={`tf-pill ${row.touchy ? "is-touchy" : "is-steady"}`}
                >
                  {row.pill}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
