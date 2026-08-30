import type { Interval } from "../api";

/**
 * Utilization is reported as a 0–1 fraction (busy time / horizon). Each row
 * shows the mean as a bar plus the confidence range as a lighter overlay,
 * so a cell that's "70% utilized, give or take a lot" reads differently
 * from one that's a tight 70%.
 */
export function UtilizationList({ utilization }: { utilization: Record<string, Interval> }) {
  const cells = Object.entries(utilization).sort(([a], [b]) => a.localeCompare(b));

  if (cells.length === 0) {
    return <div className="empty-state">No work-center activity recorded.</div>;
  }

  return (
    <div>
      {cells.map(([cell, interval]) => {
        const meanPct = interval.mean * 100;
        const loPct = interval.lo * 100;
        const hiPct = interval.hi * 100;
        return (
          <div className="util-row" key={cell}>
            <span className="util-name" title={cell}>
              {cell}
            </span>
            <div className="util-track">
              <div
                className="band-range"
                style={{
                  left: `${Math.max(0, Math.min(100, loPct))}%`,
                  width: `${Math.max(0.5, Math.min(100, hiPct) - Math.max(0, loPct))}%`,
                  top: 0,
                  bottom: 0,
                }}
              />
              <div className="util-fill" style={{ width: `${Math.max(0, Math.min(100, meanPct))}%` }} />
            </div>
            <span className="util-pct">{meanPct.toFixed(0)}%</span>
          </div>
        );
      })}
    </div>
  );
}
