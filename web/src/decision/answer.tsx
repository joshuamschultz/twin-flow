// Parts the states, improve and project views share: problem cards and a number stepper.
// The answer hero is AnswerHero.tsx; everything else is from ./primitives.
import { pct } from "./format";

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
