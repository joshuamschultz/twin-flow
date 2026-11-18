// The answer hero for pages that are not a race: an eyebrow with the question, a headline
// that is the answer, a lede, up to three figures, and an optional card on the right
// (DESIGN.md 0.1, item 3). Same markup and classes as DecisionHero.
import type { ReactNode } from "react";
import { MoneyFigure } from "./hero";

export type HeroFigure = { value: string; label: string };

export function AnswerHero(props: {
  eyebrow: string;
  headline: ReactNode;
  lede?: ReactNode;
  figures?: HeroFigure[];
  /** How sure we are, in words. */
  sure?: ReactNode;
  actions?: ReactNode;
  /** The card on the right: what changes, or the one thing to do. */
  side?: ReactNode;
  /** "h1" when the page has no other title, "h2" when it has. */
  level?: "h1" | "h2";
}) {
  const { level = "h1" } = props;
  const Title = level;
  return (
    <div className="tf-hero" id="decision" data-section="decision">
      <div className={"tf-wrap" + (props.side ? " tf-hero-grid" : "")}>
        <div>
          <div className="tf-eyebrow">{props.eyebrow}</div>
          <Title className="tf-answer">{props.headline}</Title>
          {props.lede && <p className="tf-lede">{props.lede}</p>}
          {!!props.figures?.length && (
            <div className="tf-money-row">
              {props.figures.slice(0, 3).map((f) => (
                <MoneyFigure key={f.label} value={f.value} label={f.label} />
              ))}
            </div>
          )}
          {props.sure && <div className="tf-sure">{props.sure}</div>}
          {props.actions && (
            <div className="tf-hero-actions">{props.actions}</div>
          )}
        </div>
        {props.side && <div className="tf-floor">{props.side}</div>}
      </div>
    </div>
  );
}
