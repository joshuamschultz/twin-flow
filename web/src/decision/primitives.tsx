// Shell primitives of the standard layout (DESIGN.md 9.1).
import { useContext, type MouseEvent, type ReactNode } from "react";
import { round100, signedUsd, usd } from "./format";
import { setViewParam, ShellContext } from "./viewState";

export function Section(props: {
  id: string;
  eyebrow: string;
  question: string;
  hint?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const { id, eyebrow, question, hint, actions, children } = props;
  return (
    <section
      className="tf-section"
      id={id}
      data-section={id}
      aria-labelledby={`${id}-q`}
    >
      <div className="tf-s-head">
        <div>
          <div className="tf-eyebrow">{eyebrow}</div>
          <h2 id={`${id}-q`}>{question}</h2>
          {hint && <p>{hint}</p>}
        </div>
        {actions}
      </div>
      {children}
    </section>
  );
}

export function Takeaway({
  tone,
  children,
}: {
  tone?: "info" | "warn";
  children: ReactNode;
}) {
  return (
    <div className={"tf-takeaway" + (tone === "warn" ? " tf-warn" : "")}>
      {children}
    </div>
  );
}

export function Card(props: {
  question?: string;
  sub?: ReactNode;
  takeaway?: ReactNode;
  tone?: "info" | "warn";
  className?: string;
  children?: ReactNode;
}) {
  const { question, sub, takeaway, tone, className, children } = props;
  return (
    <div className={"tf-card " + (className ?? "")}>
      {question && <h3>{question}</h3>}
      {sub && <p className="tf-sub">{sub}</p>}
      {children}
      {takeaway && <Takeaway tone={tone}>{takeaway}</Takeaway>}
    </div>
  );
}

export function Money(props: {
  value: number;
  signed?: boolean;
  round?: boolean;
}) {
  const v = props.round ? round100(props.value) : props.value;
  return <span className="tf-num">{props.signed ? signedUsd(v) : usd(v)}</span>;
}

/** A difference pill: blue when better, orange when worse, grey when neither. */
export function Delta(props: {
  value: number;
  better: "up" | "down" | "none";
  format?: (v: number) => string;
}) {
  const { value, better, format = signedUsd } = props;
  const same = Math.abs(value) < 1e-9;
  const tone =
    same || better === "none"
      ? "n"
      : (better === "up" ? value > 0 : value < 0)
        ? "g"
        : "l";
  return (
    <span className={`tf-pill tf-${tone}`}>
      {same ? "same" : format(value)}
    </span>
  );
}

export function Tag({
  kind,
  children,
}: {
  kind: "pick" | "today" | "best" | "plain";
  children: ReactNode;
}) {
  return <span className={`tf-tag tf-${kind}`}>{children}</span>;
}

export function StatusPill({
  kind,
  label,
}: {
  kind: "ok" | "risk" | "late";
  label: string;
}) {
  return <span className={`tf-status tf-${kind}`}>{label}</span>;
}

export function Segmented<T extends string>(props: {
  label: string;
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="tf-seg" role="group" aria-label={props.label}>
      {props.options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={o.value === props.value}
          onClick={() => props.onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function EmptyState({
  title,
  body,
  fix,
}: {
  title: string;
  body?: ReactNode;
  fix?: string;
}) {
  return (
    <div className="tf-empty" role="note">
      <b>{title}</b>
      {body}
      {fix && (
        <div>
          <code>{fix}</code>
        </div>
      )}
    </div>
  );
}

/** Scroll to a section. In the offline file the hash holds view state, so the
 *  section goes in `tab` instead of replacing the hash. */
export function scrollToSection(id: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  const reduce = window.matchMedia?.(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
}

export function SectionLink(props: {
  to: string;
  className?: string;
  children: ReactNode;
  current?: boolean;
}) {
  const shell = useContext(ShellContext);
  const onClick = (e: MouseEvent) => {
    e.preventDefault();
    setViewParam(shell, "tab", props.to);
    scrollToSection(props.to);
  };
  return (
    <a
      href={`#${props.to}`}
      className={props.className}
      aria-current={props.current ? "true" : undefined}
      onClick={onClick}
    >
      {props.children}
    </a>
  );
}
