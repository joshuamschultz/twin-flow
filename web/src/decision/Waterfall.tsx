// What adds up to the gain? The money parts as differences from today, gains first,
// each bar starting where the last one ended (DESIGN.md 3.3).
import { ChartFrame } from "./ChartFrame";
import { signedUsd } from "./format";

type Item = { label: string; value: number; start: number; end: number };

export function Waterfall(props: {
  items: Item[];
  total: number;
  totalLabel: string;
  min: number;
  max: number;
  label: string;
}) {
  const { items, total, totalLabel, label } = props;
  const lo = Math.min(props.min, 0);
  const span = props.max - lo || 1;
  const at = (v: number) => ((v - lo) / span) * 100;
  const table = () => (
    <table className="tf-table">
      <thead>
        <tr>
          <th scope="col">What changed</th>
          <th scope="col">Per run</th>
        </tr>
      </thead>
      <tbody>
        {items.map((i) => (
          <tr key={i.label}>
            <td>{i.label}</td>
            <td>{signedUsd(i.value)}</td>
          </tr>
        ))}
        <tr>
          <td>{totalLabel}</td>
          <td>{signedUsd(total)}</td>
        </tr>
      </tbody>
    </table>
  );
  return (
    <ChartFrame label={label} table={table}>
      {() => (
        <div>
          {items.map((i) => {
            const a = at(Math.min(i.start, i.end));
            const w = Math.abs(at(i.end) - at(i.start));
            const up = i.value > 0;
            return (
              <div className="tf-wf-row" key={i.label}>
                <div>{i.label}</div>
                <div className="tf-track">
                  <div
                    className={"tf-bar " + (up ? "tf-gain" : "tf-loss")}
                    style={{ left: `${a}%`, width: `${w}%` }}
                  />
                  <div className="tf-link" style={{ left: `${at(i.end)}%` }} />
                </div>
                <div className={"tf-val " + (up ? "tf-g" : "tf-l")}>
                  {signedUsd(i.value)}
                </div>
              </div>
            );
          })}
          <div className="tf-wf-row tf-total">
            <div>{totalLabel}</div>
            <div className="tf-track">
              <div
                className={"tf-bar " + (total >= 0 ? "tf-gain" : "tf-loss")}
                style={{
                  left: `${at(Math.min(0, total))}%`,
                  width: `${Math.abs(at(total) - at(0))}%`,
                  opacity: 0.45,
                }}
              />
            </div>
            <div className={"tf-val " + (total >= 0 ? "tf-g" : "tf-l")}>
              {signedUsd(total)}
            </div>
          </div>
        </div>
      )}
    </ChartFrame>
  );
}
