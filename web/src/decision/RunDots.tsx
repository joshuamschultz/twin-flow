// How sure are we? One dot per test run on a dollar axis that starts at "$0, same as
// today" (DESIGN.md 3.1). A band marks the likely range and a line the average.
import { useRef, type KeyboardEvent } from "react";
import { ChartFrame } from "./ChartFrame";
import { signedUsd, usd, usdCompact } from "./format";
import { dotLayout, dotsCaption } from "./race";
import { useTipProps } from "./Tooltip";

type Props = {
  gaps: number[];
  mean: number;
  low: number | null;
  high: number | null;
};

export function RunDots({ gaps, mean, low, high }: Props) {
  const tipProps = useTipProps();
  const refs = useRef<(SVGCircleElement | null)[]>([]);
  const caption = dotsCaption(gaps);
  const table = () => (
    <table className="tf-table">
      <thead>
        <tr>
          <th scope="col">Test run</th>
          <th scope="col">Gain vs today</th>
        </tr>
      </thead>
      <tbody>
        {gaps.map((g, i) => (
          <tr key={i}>
            <td>Run {i + 1}</td>
            <td>{signedUsd(g)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <>
      <ChartFrame label={caption} table={table} interactive>
        {(width) => {
          const r = 3.6;
          const probe = dotLayout(gaps, width, { r, pad: 8 });
          const top = 22;
          const base = top + Math.max(30, probe.tallest * probe.col) + 4;
          const { dots, scale } = dotLayout(gaps, width, { r, pad: 8, base });
          const x = scale.x;
          const height = base + 22;
          const sorted = [...dots].sort((a, b) => a.gap - b.gap);
          const move = (e: KeyboardEvent, at: number) => {
            const next = { ArrowRight: at + 1, ArrowUp: at + 1, ArrowLeft: at - 1, ArrowDown: at - 1, Home: 0, End: sorted.length - 1 }[e.key]; // prettier-ignore
            if (next === undefined) return;
            e.preventDefault();
            refs.current[
              Math.max(0, Math.min(sorted.length - 1, next))
            ]?.focus();
          };
          const meanX = x(mean);
          const meanRight = meanX > width - 140;
          return (
            <svg width={width} height={height}>
              <line
                x1={x(scale.min)}
                x2={x(scale.max)}
                y1={base}
                y2={base}
                style={{ stroke: "var(--tf-hero-line)" }}
              />
              {scale.ticks.map((t) => (
                <g key={t}>
                  <line
                    x1={x(t)}
                    x2={x(t)}
                    y1={base}
                    y2={base + 4}
                    style={{ stroke: "var(--tf-hero-muted)" }}
                  />
                  <text
                    x={x(t)}
                    y={base + 18}
                    className="tf-m"
                    textAnchor={
                      t === scale.min
                        ? "start"
                        : t === scale.max
                          ? "end"
                          : "middle"
                    }
                  >
                    {t === 0 ? "$0, same as today" : usdCompact(t, true)}
                  </text>
                </g>
              ))}
              {low !== null && high !== null && (
                <rect
                  x={x(low)}
                  y={top - 2}
                  width={Math.max(1, x(high) - x(low))}
                  height={base - top + 2}
                  style={{ fill: "rgba(156,194,255,.16)" }}
                />
              )}
              {sorted.map((d, k) => (
                <circle
                  key={d.index}
                  ref={(el) => {
                    refs.current[k] = el;
                  }}
                  className="tf-hit"
                  cx={d.x}
                  cy={d.y}
                  r={r}
                  tabIndex={k === 0 ? 0 : -1}
                  aria-label={`One run: ${signedUsd(d.gap)} vs today`}
                  role="img"
                  style={{
                    fill:
                      d.gap < 0 ? "var(--tf-loss)" : "var(--tf-hero-accent)",
                    stroke: "var(--tf-hero-bg)",
                    strokeWidth: 1,
                  }}
                  onKeyDown={(e) => move(e, k)}
                  {...tipProps(`One run: ${signedUsd(d.gap)} vs today`)}
                />
              ))}
              <line
                x1={meanX}
                x2={meanX}
                y1={top - 10}
                y2={base}
                style={{ stroke: "var(--tf-hero-ink)", strokeWidth: 2 }}
              />
              <text
                x={meanX + (meanRight ? -6 : 6)}
                y={top - 4}
                className="tf-m"
                textAnchor={meanRight ? "end" : "start"}
                style={{ fill: "var(--tf-hero-ink)" }}
              >
                average {signedUsd(mean)}
              </text>
            </svg>
          );
        }}
      </ChartFrame>
      <p className="tf-dots-caption">{caption}</p>
      <span className="tf-sr">Average {usd(mean)}.</span>
    </>
  );
}
