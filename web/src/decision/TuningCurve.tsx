import { useState } from "react";
import "./details.css";
import {
  fmtMoney,
  fmtMoneyShort,
  settingText,
  shortValue,
} from "./detailsFormat";
import type { CurvePanel, CurvePoint, CurveSpec } from "./tuningModel";
import { curveSpec } from "./tuningModel";
import type { Tuning } from "./types";
import { useElementWidth } from "./useElementWidth";

const PAD = { l: 52, r: 18, t: 26, b: 30 };

type Hover = { x: number; y: number; text: string } | null;

function thinBy(points: CurvePoint[], spacing: number): number {
  const longest = Math.max(...points.map((p) => shortValue(p.label).length), 1);
  return Math.max(1, Math.ceil((longest * 7 + 10) / Math.max(spacing, 1)));
}

function Panel({
  panel,
  spec,
  width,
  height,
  showY,
}: {
  panel: CurvePanel;
  spec: CurveSpec;
  width: number;
  height: number;
  showY: boolean;
}) {
  const [hover, onHover] = useState<Hover>(null);
  const left = PAD.l;
  const inner = Math.max(width - left - PAD.r, 10);
  const n = panel.points.length;
  const x = (i: number) => left + (n < 2 ? inner / 2 : (i / (n - 1)) * inner);
  const y = (v: number) =>
    PAD.t +
    (1 - (v - spec.yMin) / (spec.yMax - spec.yMin)) * (height - PAD.t - PAD.b);
  const every = thinBy(panel.points, n < 2 ? inner : inner / (n - 1));
  const path = panel.points
    .map((p, i) => `${i ? "L" : "M"}${x(i)} ${y(p.dollars as number)}`)
    .join("");
  return (
    <div className="tf-tune-svgbox">
      <svg
        className="tf-tune-svg"
        width={width}
        height={height}
        aria-hidden="true"
        focusable="false"
      >
        {spec.ticks.map((t) => (
          <g key={t}>
            <line
              x1={left}
              x2={width - PAD.r}
              y1={y(t)}
              y2={y(t)}
              className="tf-tune-grid"
            />
            {showY && (
              <text
                x={left - 8}
                y={y(t) + 4}
                textAnchor="end"
                className="tf-tune-axis"
              >
                {fmtMoneyShort(t)}
              </text>
            )}
          </g>
        ))}
        <path d={path} className="tf-tune-line" />
        {panel.points.map((p, i) => (
          <g key={`${p.label}-${i}`}>
            <circle
              cx={x(i)}
              cy={y(p.dollars as number)}
              r={p.chosen ? 6 : 4.5}
              className={p.chosen ? "tf-tune-dot is-chosen" : "tf-tune-dot"}
              data-chosen={p.chosen ? "true" : undefined}
              onPointerEnter={() =>
                onHover({
                  x: x(i),
                  y: y(p.dollars as number),
                  text: `${settingText(p.settings)}: ${fmtMoney(p.dollars as number)} per run`,
                })
              }
              onPointerLeave={() => onHover(null)}
            />
            {(i % every === 0 || p.chosen) && (
              <text
                x={x(i)}
                y={height - 10}
                textAnchor="middle"
                className="tf-tune-axis"
              >
                {shortValue(p.label)}
              </text>
            )}
            {p.chosen && (
              <text
                x={x(i)}
                y={y(p.dollars as number) - 12}
                textAnchor="middle"
                className="tf-tune-strong"
              >
                Chosen: {fmtMoney(p.dollars as number)}
              </text>
            )}
          </g>
        ))}
      </svg>
      {hover && (
        <div
          className="tf-tune-tip"
          style={{ left: hover.x, top: hover.y }}
          role="presentation"
        >
          {hover.text}
        </div>
      )}
    </div>
  );
}

function TableView({ spec, tuning }: { spec: CurveSpec; tuning: Tuning }) {
  const rows = [...spec.panels.flatMap((p) => p.points), ...spec.skipped];
  return (
    <div
      className="tf-tune-scroll"
      tabIndex={0}
      role="region"
      aria-label="Settings tried, as a table"
    >
      <table className="tf-tune-table">
        <thead>
          <tr>
            <th scope="col">Setting</th>
            <th scope="col" className="n">
              Profit per run
            </th>
            <th scope="col">Note</th>
          </tr>
        </thead>
        <tbody>
          {tuning.trials.map((t, i) => {
            const row = rows.find((r) => r.settings === t.settings);
            return (
              <tr key={i}>
                <td>{settingText(t.settings)}</td>
                <td className="n">
                  {row && row.dollars !== null
                    ? fmtMoney(row.dollars)
                    : "No result"}
                </td>
                <td>{t.chosen ? "Chosen" : (t.problem ?? "")}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Profit for each setting tried, the chosen one marked. A rule with two settings draws
 * one small line per value of the second setting. */
export function TuningCurve({
  tuning,
  label,
}: {
  tuning: Tuning;
  label: string;
}) {
  const [ref, width] = useElementWidth<HTMLDivElement>();
  const [asTable, setAsTable] = useState(false);
  const spec = curveSpec(tuning);
  const chosen = spec.panels.flatMap((p) => p.points).find((p) => p.chosen);
  const many = spec.panels.length > 1;
  const cols = many ? Math.min(spec.panels.length, width >= 640 ? 3 : 2) : 1;
  const gap = 12;
  const panelWidth = Math.floor((width - gap * (cols - 1)) / cols);
  const height = many ? 170 : 210;
  const summary =
    `Profit per run for each ${spec.xName || "setting"} tried for ${label}.` +
    (chosen
      ? ` Chosen: ${settingText(chosen.settings)}, ${fmtMoney(chosen.dollars as number)}.`
      : "");

  if (!spec.panels.some((p) => p.points.length)) {
    return (
      <div className="tf-empty" data-testid="tuning-empty">
        <b>None of the settings gave a result.</b>
        <span>Check the problems list in Details.</span>
      </div>
    );
  }
  return (
    <div className="tf-tune" ref={ref} data-testid="tuning-curve">
      <div className="tf-tune-bar">
        <button
          type="button"
          className="tf-btn-quiet"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? "Show as chart" : "Show as table"}
        </button>
      </div>
      {asTable ? (
        <TableView spec={spec} tuning={tuning} />
      ) : (
        <div className="tf-tune-chart" role="img" aria-label={summary}>
          <div
            className="tf-tune-grid-panels"
            style={{ gridTemplateColumns: `repeat(${cols}, ${panelWidth}px)` }}
          >
            {spec.panels.map((panel, i) => (
              <div
                key={panel.title ?? "one"}
                className="tf-tune-panel"
                data-testid="tuning-panel"
              >
                {panel.title && (
                  <div className="tf-tune-title">{panel.title}</div>
                )}
                <Panel
                  panel={panel}
                  spec={spec}
                  width={panelWidth}
                  height={height}
                  showY={i % cols === 0}
                />
              </div>
            ))}
          </div>
          <div className="tf-tune-xname">{spec.xName}</div>
        </div>
      )}
      {spec.skipped.length > 0 && (
        <p className="tf-note" data-testid="tuning-skipped">
          {spec.skipped.length === 1
            ? "One setting"
            : `${spec.skipped.length} settings`}{" "}
          could not run:{" "}
          {spec.skipped
            .map((p) => `${settingText(p.settings)} (${p.problem})`)
            .join("; ")}
          .
        </p>
      )}
    </div>
  );
}
