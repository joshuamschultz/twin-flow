// The run viewer's answer hero (DESIGN.md 4.1), from the shared hero parts.
import { MoneyFigure } from "../hero";
import { SectionLink } from "../primitives";
import { headlineParts, heroFigures, sideRows } from "../run";
import type { RunViewModel } from "../types";

export function RunHero({ model }: { model: RunViewModel }) {
  const { answer } = model;
  const [lead, accent] = headlineParts(answer.headline);
  const figures = heroFigures(model);
  const rows = sideRows(model);
  const tested = model.levers.some((l) => l.tested);
  return (
    <div className="tf-hero" id="answer" data-section="answer">
      <div className="tf-wrap tf-hero-grid">
        <div>
          <div className="tf-eyebrow">Will my orders finish on time?</div>
          <h1>
            {lead} {accent && <em>{accent}</em>}
          </h1>
          <p className="tf-lede">{answer.lede}</p>
          {answer.command && (
            <code className="rv-cmd rv-cmd-hero">{answer.command}</code>
          )}
          <div className="tf-money-row rv-three">
            {figures.map((f, i) => (
              <MoneyFigure
                key={f.label}
                value={f.value}
                label={f.label}
                small={i > 0}
              />
            ))}
          </div>
          <div className="tf-hero-actions">
            <SectionLink to="orders" className="tf-btn tf-primary">
              See orders at risk
            </SectionLink>
            <SectionLink to="fix" className="tf-btn">
              {tested ? "See the best fix" : "See what to test"}
            </SectionLink>
          </div>
        </div>
        <div className="tf-floor">
          <h3>The one change that helps most</h3>
          {rows.map((r) => (
            <div className="tf-floor-row" key={r.key}>
              <div>
                <div className="tf-k">{r.key}</div>
                <div className="tf-ft">
                  {r.from && <s>{r.from}</s>} {r.value}
                </div>
              </div>
              {r.delta && (
                <div className={"tf-d" + (r.warn ? " tf-warn" : "")}>
                  {r.delta}
                </div>
              )}
              {r.why && <div className="tf-why">{r.why}</div>}
              {r.command && <code className="rv-cmd">{r.command}</code>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
