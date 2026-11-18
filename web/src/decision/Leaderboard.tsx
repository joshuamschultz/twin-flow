// How does every rule compare to today? Diverging bars from today's zero line, with a
// whisker for the likely range. Weak rules sit in a fold (DESIGN.md 3.4).
import { ChartFrame } from "./ChartFrame";
import { MINUS, signedUsd, usd } from "./format";
import type { BoardRow } from "./race";
import { Tag } from "./primitives";
import { useTipProps } from "./Tooltip";

type Props = {
  main: BoardRow[];
  rest: BoardRow[];
  foldLabel: string;
  limit: number;
  label: string;
};

function axisText(t: number): string {
  if (t === 0) return "same as today";
  return (t > 0 ? "+" : MINUS) + "$" + Math.abs(t) / 1000 + "k";
}

export function Leaderboard({ main, rest, foldLabel, limit, label }: Props) {
  const tipProps = useTipProps();
  const pos = (v: number) => 50 + Math.max(-1, Math.min(1, v / limit)) * 50;
  const step = limit >= 20000 ? 10000 : limit >= 4000 ? 2000 : limit / 2;
  const ticks: number[] = [];
  for (let t = -Math.floor(limit / step) * step; t <= limit; t += step)
    ticks.push(t);
  const row = (r: BoardRow) => {
    const off = r.mean < -limit || r.mean > limit;
    const a = Math.min(pos(0), pos(r.mean));
    const w = Math.abs(pos(r.mean) - pos(0));
    return (
      <div
        key={r.id}
        className={
          "tf-lb-row" + (r.pick ? " tf-pick" : r.today ? " tf-today" : "")
        }
        tabIndex={0}
        data-rule={r.id}
        aria-label={r.tip}
        {...tipProps(r.tip)}
      >
        <div className="tf-rk">{r.rank}</div>
        <div className="tf-nm" title={r.about}>
          <span className="tf-nm-t">{r.label}</span>
          {r.pick && <Tag kind="pick">pick</Tag>}
          {r.today && <Tag kind="today">today</Tag>}
        </div>
        <div className="tf-track" aria-hidden="true">
          <div className="tf-zero" style={{ left: "50%" }} />
          {!r.today && Math.abs(r.mean) >= 1 && (
            <div
              className={"tf-bar " + (r.mean > 0 ? "tf-gain" : "tf-loss")}
              style={{ left: `${a}%`, width: `${w}%` }}
            />
          )}
          {off && (
            <div
              className="tf-cut"
              style={{ left: r.mean < 0 ? "0%" : "calc(100% - 4px)" }}
            />
          )}
          {!r.today &&
            !off &&
            r.low !== null &&
            r.high !== null &&
            Math.abs(r.mean) >= 1 && (
              <div
                className="tf-whisk"
                style={{
                  left: `${pos(r.low)}%`,
                  width: `${Math.max(0.3, pos(r.high) - pos(r.low))}%`,
                }}
              />
            )}
        </div>
        <div className={"tf-val" + (r.tone ? ` tf-${r.tone}` : "")}>
          {r.value}
        </div>
      </div>
    );
  };
  const table = () => (
    <table className="tf-table">
      <thead>
        <tr>
          <th scope="col">Rule</th>
          <th scope="col">Rank</th>
          <th scope="col">vs today, per run</th>
          <th scope="col">Likely range</th>
        </tr>
      </thead>
      <tbody>
        {[...main, ...rest].map((r) => (
          <tr key={r.id}>
            <td>{r.label}</td>
            <td>{r.rank}</td>
            <td>{r.value}</td>
            <td>
              {r.low !== null && r.high !== null
                ? `${signedUsd(r.low)} to ${signedUsd(r.high)}`
                : r.today
                  ? usd(0)
                  : ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <ChartFrame label={label} table={table} interactive>
      {() => (
        <div>
          <div className="tf-lb-axis" aria-hidden="true">
            <span />
            <span />
            <div className="tf-ax">
              {ticks.map((t) => (
                <span key={t} style={{ left: `${pos(t)}%` }}>
                  {axisText(t)}
                </span>
              ))}
            </div>
            <span style={{ textAlign: "right" }}>vs today, per run</span>
          </div>
          {main.map(row)}
          {rest.length > 0 && (
            <details className="tf-more">
              <summary>{foldLabel}</summary>
              {rest.map(row)}
            </details>
          )}
        </div>
      )}
    </ChartFrame>
  );
}
