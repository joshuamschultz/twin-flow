// The one answer hero for every view that is not a race (DESIGN.md 0.1, item 3): an eyebrow
// with the question, a headline that is the answer, a lede, up to three figures, how sure we
// are, actions, and an optional card on the right. Same markup and classes as DecisionHero.
import type { ReactNode } from "react";
import { ConfidenceBadge, MoneyFigure } from "./hero";

/** A hero figure. Only the first is big unless `small` says otherwise. */
export type HeroFigure = { value: string; label: string; small?: boolean };

export function AnswerHero(props: {
  eyebrow: string;
  headline: ReactNode;
  lede?: ReactNode;
  figures?: HeroFigure[];
  /** "three" gives three figures their own columns (run and sweep views). */
  figuresLayout?: "row" | "three";
  /** How sure we are, as a badge from the view model. */
  badge?: { words: string; level: string; detail?: string | null };
  /** How sure we are, in free words, when there is no badge. */
  sure?: ReactNode;
  actions?: ReactNode;
  /** The card on the right: what changes, or the one thing to do. */
  side?: ReactNode;
  /** "floor" for a what-changes card, "aside" for a plain note card. */
  sideKind?: "floor" | "aside";
  /** The section id the section bar links to. */
  id?: string;
  /** "h1" when the page has no other title, "h2" when it has. */
  level?: "h1" | "h2";
  /** Shown under the lede, for example a command to run. */
  children?: ReactNode;
}) {
  const { level = "h1", id = "decision", sideKind = "floor" } = props;
  const Title = level;
  return (
    <div className="tf-hero" id={id} data-section={id}>
      <div className={"tf-wrap" + (props.side ? " tf-hero-grid" : "")}>
        <div>
          <div className="tf-eyebrow">{props.eyebrow}</div>
          <Title className="tf-answer">{props.headline}</Title>
          {props.lede && <p className="tf-lede">{props.lede}</p>}
          {props.children}
          {!!props.figures?.length && (
            <div
              className={
                "tf-money-row" +
                (props.figuresLayout === "three" ? " tf-three" : "")
              }
            >
              {props.figures.slice(0, 3).map((f) => (
                <MoneyFigure
                  key={f.label}
                  value={f.value}
                  label={f.label}
                  small={f.small}
                />
              ))}
            </div>
          )}
          {(props.badge || props.sure) && (
            <div className="tf-sure">
              {props.badge && <ConfidenceBadge {...props.badge} />}
              {props.sure}
            </div>
          )}
          {props.actions && (
            <div className="tf-hero-actions">{props.actions}</div>
          )}
        </div>
        {props.side &&
          (sideKind === "aside" ? (
            <aside className="tf-aside">{props.side}</aside>
          ) : (
            <div className="tf-floor">{props.side}</div>
          ))}
      </div>
    </div>
  );
}
