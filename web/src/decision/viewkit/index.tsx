// The shared page parts for the states, improve and project views (DESIGN.md 0.1):
// top bar, sticky section bar, hero, sections and cards, pills, problems.
import type { ReactNode } from "react";
import "./viewkit.css";
import { sentence } from "./format";

export type SectionLink = { id: string; label: string };

function toggleTheme(): void {
  const root = document.documentElement;
  const dark =
    root.dataset.theme === "dark" ||
    (root.dataset.theme !== "light" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches);
  root.dataset.theme = dark ? "light" : "dark";
}

export function Page(props: {
  view: string;
  viewName: string;
  project: string;
  scope: string;
  sections: SectionLink[];
  children: ReactNode;
}) {
  return (
    <div className="vk-page tf-root" data-view={props.view}>
      <header className="vk-top">
        <div className="vk-wrap vk-top-row">
          <span className="vk-brand">
            twin<i>flow</i>
          </span>
          <span className="vk-crumb">
            <b>{props.viewName}</b> . {props.project}
            {props.scope ? ` . ${props.scope}` : ""}
          </span>
          <span className="vk-spacer" />
          <button
            type="button"
            className="vk-btn vk-print-hide"
            onClick={toggleTheme}
          >
            Light or dark
          </button>
          <button
            type="button"
            className="vk-btn vk-print-hide"
            onClick={() => window.print()}
          >
            Print
          </button>
        </div>
        <nav className="vk-wrap vk-tabs" aria-label="Sections">
          {props.sections.map((s) => (
            <a key={s.id} href={`#${s.id}`}>
              {s.label}
            </a>
          ))}
        </nav>
      </header>
      <main>{props.children}</main>
    </div>
  );
}

export function Hero(props: {
  eyebrow: string;
  headline: string;
  lede?: ReactNode;
  badge?: { text: string; warn?: boolean };
  aside?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <section className="vk-hero" id="answer">
      <div className="vk-wrap vk-hero-grid">
        <div>
          <p className="vk-eyebrow">{props.eyebrow}</p>
          <h1>{props.headline}</h1>
          {props.lede ? <p className="vk-lede">{props.lede}</p> : null}
          {props.badge ? (
            <p style={{ marginTop: 16 }}>
              <span className={`vk-badge${props.badge.warn ? " warn" : ""}`}>
                {props.badge.text}
              </span>
            </p>
          ) : null}
          {props.children}
        </div>
        {props.aside ? <aside className="vk-aside">{props.aside}</aside> : null}
      </div>
    </section>
  );
}

export function Section(props: {
  id: string;
  eyebrow: string;
  title: string;
  intro?: string;
  children: ReactNode;
}) {
  return (
    <section
      className="vk-section"
      id={props.id}
      aria-labelledby={`${props.id}-h`}
    >
      <div className="vk-wrap">
        <div className="vk-s-head">
          <p className="vk-eyebrow">{props.eyebrow}</p>
          <h2 id={`${props.id}-h`}>{props.title}</h2>
          {props.intro ? <p>{props.intro}</p> : null}
        </div>
        {props.children}
      </div>
    </section>
  );
}

export function Card(props: {
  title: string;
  sub?: string;
  takeaway?: string;
  children: ReactNode;
}) {
  return (
    <div className="vk-card">
      <h3>{props.title}</h3>
      {props.sub ? <p className="vk-sub">{props.sub}</p> : null}
      {props.children}
      {props.takeaway ? <p className="vk-takeaway">{props.takeaway}</p> : null}
    </div>
  );
}

export function Pill(props: {
  tone: "good" | "bad" | "neutral";
  children: ReactNode;
}) {
  return <span className={`vk-pill ${props.tone}`}>{props.children}</span>;
}

/** A dashed box for a section with no data: what is missing, and the exact fix. */
export function Missing(props: { said: string; fix?: string }) {
  return (
    <div className="vk-dashed" role="note">
      <b>{props.said}</b>
      {props.fix ? <p style={{ marginTop: 6 }}>{props.fix}</p> : null}
    </div>
  );
}

export type ProblemRow = {
  where?: string | null;
  file?: string | null;
  line?: number | null;
  severity?: string;
  message: string;
  fix: string;
};

export function Problems(props: { problems: ProblemRow[] }) {
  return (
    <ol className="vk-list" style={{ paddingLeft: 0 }}>
      {props.problems.map((p, i) => {
        const place = p.where ?? p.file ?? "";
        const where = p.line ? `${place} line ${p.line}` : place;
        return (
          <li className="vk-problem" key={`${i}-${p.message}`}>
            {where ? <p className="where">{`${i + 1}. ${where}`}</p> : null}
            <p>{sentence(p.message)}</p>
            <p className="fix">
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
  return (
    <div className="vk-field" role="group" aria-label={props.label}>
      <span>{props.label}</span>
      <span className="vk-step">
        <button
          type="button"
          className="vk-btn"
          aria-label={`Less: ${props.label}`}
          disabled={props.value <= props.min}
          onClick={() =>
            props.onChange(clamp(+(props.value - step).toFixed(4)))
          }
        >
          {"−"}
        </button>
        <output aria-live="polite">{show(props.value)}</output>
        <button
          type="button"
          className="vk-btn"
          aria-label={`More: ${props.label}`}
          disabled={props.value >= props.max}
          onClick={() =>
            props.onChange(clamp(+(props.value + step).toFixed(4)))
          }
        >
          +
        </button>
      </span>
    </div>
  );
}
