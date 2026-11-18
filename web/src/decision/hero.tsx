// The answer hero of the race view (DESIGN.md 3.1): what to do, what it is worth, how
// sure we are, the top caution and what changes on the floor.
import type { ReactNode } from "react";
import { SectionLink } from "./primitives";
import { cautionLine, floorValues, hero as heroWords } from "./race";
import { RunDots } from "./RunDots";
import type { FloorRow, RaceViewModel } from "./types";

export function MoneyFigure(props: {
  value: string;
  label: string;
  small?: boolean;
  check?: string;
}) {
  return (
    <div className="tf-money">
      <div className={"tf-v" + (props.small ? " tf-small" : "")}>
        {props.value}
      </div>
      <div className="tf-k">{props.label}</div>
      {props.check && <div className="tf-check">{props.check}</div>}
    </div>
  );
}

export function ConfidenceBadge(props: {
  level: string;
  words: string;
  detail?: string | null;
}) {
  const warn =
    props.level === "very_likely_worse" || props.level === "maybe_worse";
  const maybe = props.level.startsWith("maybe") || props.level === "unknown";
  return (
    <div className="tf-sure-head">
      <span
        className={
          "tf-badge" + (warn ? " tf-warn" : "") + (maybe ? " tf-maybe" : "")
        }
      >
        <span aria-hidden="true">●</span> {props.words}
      </span>
      {props.detail && <span className="tf-t">{props.detail}</span>}
    </div>
  );
}

export function CautionLine({ model }: { model: RaceViewModel }) {
  const line = cautionLine(model);
  if (!line) return null;
  return (
    <div className="tf-caution">
      <b>One caution.</b> {line.title} {line.text}{" "}
      <SectionLink to={line.link.to}>{line.link.label}</SectionLink>
    </div>
  );
}

export function FloorChanges({ rows }: { rows: FloorRow[] }) {
  if (!rows.length) return null;
  return (
    <div className="tf-floor">
      <h3>What changes on the floor</h3>
      {rows.slice(0, 5).map((r) => {
        const [before, after] = floorValues(r);
        return (
          <div className="tf-floor-row" key={r.key}>
            <div>
              <div className="tf-k">{r.label}</div>
              <div className="tf-ft">
                <s>
                  {before} <span aria-hidden="true">→</span>
                  <span className="tf-sr"> to </span>
                </s>{" "}
                {after}
              </div>
            </div>
            <div className={"tf-d" + (r.attention ? " tf-warn" : "")}>
              {r.short}
            </div>
            <div className="tf-why">{r.note}</div>
          </div>
        );
      })}
    </div>
  );
}

export function DecisionHero({
  model,
  actions,
}: {
  model: RaceViewModel;
  actions: ReactNode;
}) {
  const h = heroWords(model);
  const c = model.confidence;
  const gaps = c?.gaps ?? [];
  const check = model.delta?.long_check?.words;
  return (
    <div className="tf-hero" id="decision" data-section="decision">
      <div className="tf-wrap tf-hero-grid">
        <div>
          <div className="tf-eyebrow">Recommendation · what to make next</div>
          <h1>
            {h.before}
            <em>{h.name}</em>
            {h.after}
          </h1>
          <p className="tf-lede">
            {h.lede.map((p, i) =>
              p.strong ? (
                <b key={i}>{p.text}</b>
              ) : (
                <span key={i}>{p.text}</span>
              ),
            )}
          </p>
          {h.perRun && (
            <div className="tf-money-row">
              <MoneyFigure
                value={h.perRun}
                label={`more profit per ${h.horizon}-day run`}
              />
              {h.perYear && (
                <MoneyFigure
                  small
                  value={h.perYear}
                  label={`more per year, if every ${h.horizon} days looks like these runs`}
                  check={check}
                />
              )}
            </div>
          )}
          {h.note && <p className="tf-hero-note">{h.note}</p>}
          {h.badge && (
            <div className="tf-sure">
              <ConfidenceBadge
                level={h.level}
                words={h.badge}
                detail={h.range}
              />
              {gaps.length > 1 && c && (
                <div style={{ marginTop: 10 }}>
                  <RunDots
                    gaps={gaps}
                    mean={c.mean ?? 0}
                    low={c.low}
                    high={c.high}
                  />
                </div>
              )}
              <CautionLine model={model} />
            </div>
          )}
          <div className="tf-hero-actions">{actions}</div>
        </div>
        <FloorChanges rows={model.floor} />
      </div>
    </div>
  );
}
