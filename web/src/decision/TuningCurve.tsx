import "./details.css";
import { ChartFrame } from "./ChartFrame";
import { usd, usdCompact } from "./format";
import { settingText, shortValue } from "./settings";
import { useTipProps } from "./Tooltip";
import type { CurvePanel, CurveSpec } from "./tuningModel";
import { curveSpec } from "./tuningModel";
import type { Tuning } from "./types";

const PAD = { l: 52, r: 18, t: 26, b: 30 };

function thinBy(labels: string[], spacing: number): number {
  const longest = Math.max(...labels.map((l) => shortValue(l).length), 1);
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
  const tip = useTipProps();
  const left = PAD.l;
  const inner = Math.max(width - left - PAD.r, 10);
  const n = panel.points.length;
  const x = (i: number) => left + (n < 2 ? inner / 2 : (i / (n - 1)) * inner);
  const y = (v: number) =>
    PAD.t +
    (1 - (v - spec.yMin) / (spec.yMax - spec.yMin)) * (height - PAD.t - PAD.b);
  const every = thinBy(
    panel.points.map((p) => p.label),
    n < 2 ? inner : inner / (n - 1),
  );
  const path = panel.points
    .map((p, i) => `${i ? "L" : "M"}${x(i)} ${y(p.dollars as number)}`)
    .join("");
  return (
    <svg className="tf-tune-svg" width={width} height={height}>
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
            <text x={left - 8} y={y(t) + 4} textAnchor="end" className="tf-m">
              {usdCompact(t)}
            </text>
          )}
        </g>
      ))}
      <path d={path} className="tf-tune-line" />
      {panel.points.map((p, i) => {
        const text = `${settingText(p.settings)}: ${usd(p.dollars as number)} per run`;
        return (
          <g key={`${p.label}-${i}`}>
            <circle
              cx={x(i)}
              cy={y(p.dollars as number)}
              r={p.chosen ? 6 : 4.5}
              className={"tf-hit tf-tune-dot" + (p.chosen ? " tf-chosen" : "")}
              data-chosen={p.chosen ? "true" : undefined}
              tabIndex={0}
              role="img"
              aria-label={text + (p.chosen ? ". Chosen." : "")}
              {...tip(text)}
            />
            {(i % every === 0 || p.chosen) && (
              <text
                x={x(i)}
                y={height - 10}
                textAnchor="middle"
                className="tf-m"
              >
                {shortValue(p.label)}
              </text>
            )}
            {p.chosen && (
              <text
                x={x(i)}
                y={y(p.dollars as number) - 12}
                textAnchor="middle"
                className="tf-strong"
              >
                Chosen: {usd(p.dollars as number)}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

function TrialsTable({ tuning }: { tuning: Tuning }) {
  return (
    <table className="tf-table" aria-label="Settings tried">
      <thead>
        <tr>
          <th scope="col">Setting</th>
          <th scope="col">Profit per run</th>
          <th scope="col">Note</th>
        </tr>
      </thead>
      <tbody>
        {tuning.trials.map((t, i) => (
          <tr key={i}>
            <td>{settingText(t.settings)}</td>
            <td>{t.dollars === null ? "No result" : usd(t.dollars)}</td>
            <td>{t.chosen ? "Chosen" : (t.problem ?? "")}</td>
          </tr>
        ))}
      </tbody>
    </table>
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
  const spec = curveSpec(tuning);
  const chosen = spec.panels.flatMap((p) => p.points).find((p) => p.chosen);
  const many = spec.panels.length > 1;
  const summary =
    `Profit per run for each ${spec.xName || "setting"} tried for ${label}.` +
    (chosen
      ? ` Chosen: ${settingText(chosen.settings)}, ${usd(chosen.dollars as number)}.`
      : "");
  if (!spec.panels.some((p) => p.points.length)) {
    return (
      <div className="tf-empty" data-testid="tuning-empty">
        <b>None of the settings gave a result.</b>
        Check the problems list in Details.
      </div>
    );
  }
  return (
    <div data-testid="tuning-curve">
      <ChartFrame
        label={summary}
        interactive
        table={() => <TrialsTable tuning={tuning} />}
      >
        {(width) => {
          const cols = many
            ? Math.min(spec.panels.length, width >= 640 ? 3 : 2)
            : 1;
          const gap = 12;
          const panelWidth = Math.floor((width - gap * (cols - 1)) / cols);
          return (
            <>
              <div
                className="tf-tune-panels"
                style={{
                  gridTemplateColumns: `repeat(${cols}, ${panelWidth}px)`,
                }}
              >
                {spec.panels.map((panel, i) => (
                  <div key={panel.title ?? "one"} data-testid="tuning-panel">
                    {panel.title && (
                      <div className="tf-tune-title">{panel.title}</div>
                    )}
                    <Panel
                      panel={panel}
                      spec={spec}
                      width={panelWidth}
                      height={many ? 170 : 210}
                      showY={i % cols === 0}
                    />
                  </div>
                ))}
              </div>
              <div className="tf-tune-xname">{spec.xName}</div>
            </>
          );
        }}
      </ChartFrame>
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
