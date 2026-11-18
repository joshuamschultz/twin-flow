// "What one change would help most?" (DESIGN.md 4.4). Tested changes, best first.
import { EmptyState, Tag } from "../primitives";
import { pct } from "../run";
import type { RunLever } from "../types";

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
    <div className="rv-levers">
      {tested.length === 0 && (
        <EmptyState
          title="No fixes tested yet."
          body={<p>Test a change with a sweep, then make this report again.</p>}
        />
      )}
      {tested.length > 0 && (
        <ol
          className="rv-lever-list"
          aria-label={`${tested.length} tested changes, best first`}
        >
          {tested.map((l, i) => {
            const on = l.on_time ?? 0;
            const today = base ?? on;
            const lo = Math.min(today, on);
            const hi = Math.max(today, on);
            const gain = l.gain_points ?? (on - today) * 100;
            return (
              <li className="rv-lever" key={l.id}>
                <div className="rv-rank tf-num">{i + 1}</div>
                <div>
                  <b className="rv-lever-name">{l.label}</b>
                  {i === 0 && gain > 0 && <Tag kind="best">best</Tag>}
                  <div className="tf-note">{l.note}</div>
                </div>
                <div className="rv-lv-bar">
                  <div className="tf-track rv-track-lever">
                    <div
                      className="tf-bar rv-today"
                      style={{ left: 0, width: `${lo * 100}%` }}
                    />
                    <div
                      className={`tf-bar ${gain >= 0 ? "tf-gain" : "tf-loss"}`}
                      style={{
                        left: `${lo * 100}%`,
                        width: `${(hi - lo) * 100}%`,
                      }}
                    />
                  </div>
                  <div className="tf-note">
                    {pct(today, 0)} today → {pct(on, 0)} on time
                  </div>
                </div>
                <div className={`tf-val ${gain >= 0 ? "tf-g" : "tf-l"}`}>
                  {gain >= 0 ? "+" : "−"}
                  {Math.abs(gain).toFixed(1)} pts
                </div>
              </li>
            );
          })}
        </ol>
      )}
      {ideas.length > 0 && (
        <div className="rv-ideas">
          <p className="tf-note">
            Not tested yet: {ideas.map((l) => l.label.toLowerCase()).join(", ")}
            . In the workspace, pick one and press Test. Here, run the command:
          </p>
          {ideas.map((l) => (
            <div key={l.id} className="rv-idea">
              <b>{l.label}</b>
              <p className="tf-note">{l.note}</p>
              {l.command && <code className="rv-cmd">{l.command}</code>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
