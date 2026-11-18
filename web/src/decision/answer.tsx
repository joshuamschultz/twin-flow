// Parts the states, improve and project views share that the standard layout has no component for: a plain
// answer hero, problem cards and a number stepper. Everything else is from ../primitives.
import type { ReactNode } from "react";
import { pct } from "./format";
import { ConfidenceBadge } from "./hero";

/** The navy answer band (DESIGN.md 0.1 item 3) for views that have no money figure. */
export function AnswerHero(props: {
  id?: string;
  eyebrow: string;
  headline: string;
  lede?: ReactNode;
  badge?: { words: string; level: string; detail?: string | null };
  aside?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div
      className="tf-hero"
      id={props.id ?? "answer"}
      data-section={props.id ?? "answer"}
    >
      <div className="tf-wrap tf-hero-grid">
        <div>
          <div className="tf-eyebrow">{props.eyebrow}</div>
          <h1>{props.headline}</h1>
          {props.lede && <p className="tf-lede">{props.lede}</p>}
          {props.badge && (
            <div className="tf-sure">
              <ConfidenceBadge {...props.badge} />
            </div>
          )}
          {props.children}
        </div>
        {props.aside && <aside className="tf-aside">{props.aside}</aside>}
      </div>
    </div>
  );
}

export type ProblemRow = {
  where?: string | null;
  file?: string | null;
  line?: number | null;
  message: string;
  fix: string;
};

export function Problems({ problems }: { problems: ProblemRow[] }) {
  return (
    <ol className="tf-list" style={{ paddingLeft: 0 }}>
      {problems.map((p, i) => {
        const place = p.where ?? p.file ?? "";
        const where = p.line ? `${place} line ${p.line}` : place;
        return (
          <li className="tf-problem" key={`${i}-${p.message}`}>
            {where && <p className="tf-where">{`${i + 1}. ${where}`}</p>}
            <p>{sentence(p.message)}</p>
            <p className="tf-fix">
              <b>Fix:</b> {p.fix}
            </p>
          </li>
        );
      })}
    </ol>
  );
}

export function Stepper(props: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  format?: (n: number) => string;
  onChange: (n: number) => void;
}) {
  const step = props.step ?? 1;
  const show = props.format ?? String;
  const clamp = (n: number) => Math.min(props.max, Math.max(props.min, n));
  const move = (by: number) =>
    props.onChange(clamp(+(props.value + by).toFixed(4)));
  return (
    <div className="tf-field" role="group" aria-label={props.label}>
      <span>{props.label}</span>
      <span className="tf-step">
        <button
          type="button"
          className="tf-btn"
          aria-label={`Less: ${props.label}`}
          disabled={props.value <= props.min}
          onClick={() => move(-step)}
        >
          {"−"}
        </button>
        <output aria-live="polite">{show(props.value)}</output>
        <button
          type="button"
          className="tf-btn"
          aria-label={`More: ${props.label}`}
          disabled={props.value >= props.max}
          onClick={() => move(step)}
        >
          +
        </button>
      </span>
    </div>
  );
}

/** "1 problem" or "2 problems". */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** A 0..1 share as a whole percent, with one decimal under 1%: "46%", "0.7%". */
export function share(value: number): string {
  return pct(value, value > 0 && value < 0.01 ? 1 : 0);
}

export function sentence(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}
