// When does it pull ahead of today? Profit so far minus today's rule, by day. Today is
// the dashed zero line (DESIGN.md 3.3). Hand-built SVG: no chart library in the file.
import { useState, type KeyboardEvent, type MouseEvent } from "react";
import { ChartFrame } from "./ChartFrame";
import { signedUsd, usd, usdCompact } from "./format";
import { dollarScale, labelEnds, type Series } from "./race";
import { useTip } from "./Tooltip";
import type { Timing } from "./types";

type Props = {
  series: Series[];
  timing: Timing;
  label: string;
  todayLabel?: string;
};

export function GapLine({
  series,
  timing,
  label,
  todayLabel = "Today",
}: Props) {
  const tip = useTip();
  const [day, setDay] = useState<number | null>(null);
  const days = series[0]?.values.length ?? 0;
  const pick = series[0];
  const band = timing.gap_by_day;
  const say = (d: number) => {
    const v = pick.values[d - 1];
    const where =
      Math.abs(v) < 1
        ? "level with today"
        : v > 0
          ? `${usd(v)} ahead of today`
          : `${usd(-v)} behind today`;
    const lo = band.low[d - 1];
    const hi = band.high[d - 1];
    const range =
      lo !== undefined && hi !== undefined
        ? ` Most runs: ${signedUsd(lo)} to ${signedUsd(hi)}.`
        : "";
    return `Day ${d}: ${pick.label} is ${where}.${range}`;
  };
  const table = () => (
    <table className="tf-table">
      <thead>
        <tr>
          <th scope="col">Day</th>
          {series.map((s) => (
            <th scope="col" key={s.id}>
              {s.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {Array.from({ length: days }, (_, i) => (
          <tr key={i}>
            <td>Day {i + 1}</td>
            {series.map((s) => (
              <td key={s.id}>{signedUsd(s.values[i])}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <ChartFrame label={label} table={table} interactive>
      {(width) => {
        const H = 250;
        const l = 52;
        const r = width < 520 ? 112 : 136;
        const t = 14;
        const b = 28;
        const all = series.flatMap((s) => s.values);
        const ys = dollarScale(all, H - t - b, 0);
        const y = (v: number) => t + (H - t - b) - (ys.x(v) - 0);
        const x = (i: number) =>
          l + (i / Math.max(1, days - 1)) * (width - l - r);
        const path = (vals: number[]) =>
          vals
            .map(
              (v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`,
            )
            .join("");
        const ends = labelEnds(
          [y(0), ...series.map((s) => y(s.values[days - 1]))].map((v) => v + 4),
          14,
        );
        const low = timing.lowest;
        const pass = timing.pass_day;
        const dayTicks = [
          1,
          ...[0.25, 0.5, 0.75].map((f) => Math.round(days * f)),
          days,
        ];
        const pick = (e: MouseEvent<SVGRectElement>) => {
          const box = e.currentTarget.getBoundingClientRect();
          const i = Math.round(
            ((e.clientX - box.left) / box.width) * (days - 1),
          );
          const d = Math.max(1, Math.min(days, i + 1));
          setDay(d);
          tip.show(say(d), e.clientX, e.clientY);
        };
        const key = (e: KeyboardEvent<SVGRectElement>) => {
          const cur = day ?? 1;
          const next = { ArrowRight: cur + 1, ArrowLeft: cur - 1, PageUp: cur + 10, PageDown: cur - 10, Home: 1, End: days }[e.key]; // prettier-ignore
          if (next === undefined) return;
          e.preventDefault();
          const d = Math.max(1, Math.min(days, next));
          setDay(d);
          const box = e.currentTarget.getBoundingClientRect();
          tip.show(
            say(d),
            box.left + ((d - 1) / (days - 1)) * box.width,
            box.top + y(pick0(d)) - t,
          );
        };
        const pick0 = (d: number) => series[0].values[d - 1];
        return (
          <svg width={width} height={H}>
            {ys.ticks.map((tv) => (
              <g key={tv}>
                <line
                  x1={l}
                  x2={width - r}
                  y1={y(tv)}
                  y2={y(tv)}
                  style={{
                    stroke: tv === 0 ? "var(--tf-axis)" : "var(--tf-grid)",
                    strokeDasharray: tv === 0 ? "4 3" : undefined,
                  }}
                />
                <text x={l - 6} y={y(tv) + 4} textAnchor="end" className="tf-m">
                  {usdCompact(tv, true)}
                </text>
              </g>
            ))}
            <text
              x={width - r + 8}
              y={ends[0]}
              style={{ fill: "var(--tf-today)" }}
            >
              {todayLabel}
            </text>
            {dayTicks.map((d) => (
              <text
                key={d}
                x={x(d - 1)}
                y={H - 8}
                textAnchor="middle"
                className="tf-m"
              >
                day {d}
              </text>
            ))}
            {[...series].reverse().map((s) => (
              <path
                key={s.id}
                d={path(s.values)}
                style={{
                  fill: "none",
                  stroke: s.emphasis ? "var(--tf-pick)" : "var(--tf-today)",
                  strokeWidth: s.emphasis ? 2.5 : 1.5,
                  strokeLinejoin: "round",
                }}
              />
            ))}
            {series.map((s, i) => (
              <text
                key={s.id}
                x={width - r + 8}
                y={ends[i + 1]}
                className={s.emphasis ? "tf-strong" : undefined}
              >
                {s.label}
              </text>
            ))}
            {low.dollars < 0 && low.day <= days && (
              <g>
                <circle
                  cx={x(low.day - 1)}
                  cy={y(low.dollars)}
                  r={4}
                  style={{
                    fill: "var(--tf-loss)",
                    stroke: "var(--ba-surface)",
                    strokeWidth: 2,
                  }}
                />
                <text x={x(low.day - 1) + 8} y={y(low.dollars) + 14}>
                  Day {low.day}: {usd(-low.dollars)} behind
                </text>
              </g>
            )}
            {pass !== null && pass <= days && (
              <g>
                <circle
                  cx={x(pass - 1)}
                  cy={y(pick0(pass))}
                  r={4}
                  style={{
                    fill: "var(--tf-pick)",
                    stroke: "var(--ba-surface)",
                    strokeWidth: 2,
                  }}
                />
                <text
                  x={x(pass - 1) - 8}
                  y={y(pick0(pass)) - 8}
                  textAnchor="end"
                >
                  Passes today on day {pass}
                </text>
              </g>
            )}
            {day !== null && (
              <line
                x1={x(day - 1)}
                x2={x(day - 1)}
                y1={t}
                y2={H - b}
                style={{ stroke: "var(--tf-axis)" }}
              />
            )}
            <rect
              className="tf-hit"
              x={l}
              y={t}
              width={Math.max(1, width - l - r)}
              height={H - t - b}
              style={{ fill: "transparent" }}
              tabIndex={0}
              role="slider"
              aria-label="Day on the chart"
              aria-valuemin={1}
              aria-valuemax={days}
              aria-valuenow={day ?? 1}
              aria-valuetext={say(day ?? 1)}
              onMouseMove={pick}
              onClick={pick}
              onMouseLeave={() => {
                setDay(null);
                tip.hide();
              }}
              onKeyDown={key}
              onBlur={() => {
                setDay(null);
                tip.hide();
              }}
            />
          </svg>
        );
      }}
    </ChartFrame>
  );
}
