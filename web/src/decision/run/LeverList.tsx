// "What one change would help most?" (DESIGN.md 4.4). Tested changes, best first.
import type { RunLever } from "../types";
import { pct } from "./runText";

export function LeverList({
  levers,
  base,
}: {
  levers: RunLever[];
  base: number | null;
}) {
  const tested = levers.filter((l) => l.tested && l.on_time != null);
  const ideas = levers.filter((l) => !l.tested);
  return (
    <div
      className="rv-levers"
      aria-label={`${tested.length} tested changes, best first`}
    >
      {tested.length === 0 && (
        <div className="rv-empty">
          <b>No fixes tested yet.</b> Test a change with a sweep, then make this
          report again.
        </div>
      )}
      {tested.map((l, i) => {
        const on = l.on_time ?? 0;
        const today = base ?? on;
        const lo = Math.min(today, on);
        const hi = Math.max(today, on);
        const gain = l.gain_points ?? (on - today) * 100;
        return (
          <div className="rv-lever" key={l.id}>
            <div className="rv-rank rv-num">{i + 1}</div>
            <div>
              <b className="rv-lever-name">{l.label}</b>
              {i === 0 && gain > 0 && <span className="rv-tag">best</span>}
              <div className="rv-note">{l.note}</div>
            </div>
            <div className="rv-lv-bar">
              <div
                className="rv-track rv-track-lever"
                role="img"
                aria-label={`${pct(today)} today to ${pct(on)} on time`}
              >
                <div
                  className="rv-bar rv-today"
                  style={{ left: 0, width: `${lo * 100}%` }}
                />
                <div
                  className={`rv-bar ${gain >= 0 ? "rv-gain" : "rv-loss"}`}
                  style={{ left: `${lo * 100}%`, width: `${(hi - lo) * 100}%` }}
                />
              </div>
              <div className="rv-note">
                {pct(today, 0)} today → {pct(on, 0)} on time
              </div>
            </div>
            <div className={`rv-val ${gain >= 0 ? "rv-g" : "rv-l"}`}>
              {gain >= 0 ? "+" : "−"}
              {Math.abs(gain).toFixed(1)} pts
            </div>
          </div>
        );
      })}
      {ideas.length > 0 && (
        <div className="rv-ideas">
          <p className="rv-note">
            Not tested yet: {ideas.map((l) => l.label.toLowerCase()).join(", ")}
            . In the workspace, pick one and press Test. Here, run the command:
          </p>
          {ideas.map((l) => (
            <div key={l.id} className="rv-idea">
              <b>{l.label}</b>
              <p className="rv-note">{l.note}</p>
              {l.command && <code className="rv-cmd">{l.command}</code>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
