import "./details.css";
import { usd } from "./format";
import { Tag } from "./primitives";
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
      className="tf-scroll"
      tabIndex={0}
      role="region"
      aria-label="How touchy each rule is"
    >
      <table className="tf-table tf-tune-table" data-testid="tuning-table">
        <thead>
          <tr>
            <th scope="col">Rule</th>
            <th scope="col">Tried</th>
            <th scope="col">Best setting</th>
            <th scope="col">Best to worst</th>
            <th scope="col">
              <span className="tf-sr">Steady or touchy</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr
              key={row.policy}
              className={row.policy === selected ? "tf-selected" : undefined}
            >
              <th scope="row">
                {onSelect ? (
                  <button
                    type="button"
                    className="tf-linkbtn"
                    aria-pressed={row.policy === selected}
                    onClick={() => onSelect(row.policy)}
                  >
                    {row.label}
                  </button>
                ) : (
                  row.label
                )}
                {row.isPick && <Tag kind="pick">pick</Tag>}
              </th>
              <td>{row.tried}</td>
              <td className="tf-wrapcell">{row.best}</td>
              <td>{usd(row.spread)}</td>
              <td>
                <span className={`tf-pill ${row.touchy ? "tf-l" : "tf-n"}`}>
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
