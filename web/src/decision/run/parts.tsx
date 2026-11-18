// Shared page parts for the run viewer and the sweep view (DESIGN.md 0.1 standard layout).
import { useEffect, useState, type ReactNode } from "react";

export type SectionLink = { id: string; label: string };

function useScrollSpy(ids: string[]): string {
  const [on, setOn] = useState(ids[0]);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setOn(e.target.id);
      },
      { rootMargin: "-110px 0px -65% 0px" },
    );
    for (const id of ids) {
      const el = document.getElementById(id);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
  }, [ids.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  return on;
}

export function initialTheme(): void {
  const q =
    new URLSearchParams(location.search).get("theme") ??
    new URLSearchParams(location.hash.slice(1)).get("theme");
  if (q === "dark" || q === "light") document.documentElement.dataset.theme = q;
}

export function TopBar({
  view,
  scope,
  detail,
  sections,
}: {
  view: string;
  scope: string;
  detail: string;
  sections: SectionLink[];
}) {
  const on = useScrollSpy(sections.map((s) => s.id));
  const toggle = () => {
    const root = document.documentElement;
    const dark =
      root.dataset.theme === "dark" ||
      (!root.dataset.theme &&
        matchMedia("(prefers-color-scheme: dark)").matches);
    root.dataset.theme = dark ? "light" : "dark";
  };
  return (
    <header className="rv-top">
      <div className="rv-wrap rv-top-row">
        <span className="rv-brand">
          twinflow<i>.</i>
        </span>
        <span className="rv-crumb">
          <b>{view}</b> · {scope} <span className="rv-opt">· {detail}</span>
        </span>
        <span className="rv-spacer" />
        <button
          type="button"
          className="rv-btn"
          onClick={toggle}
          aria-label="Switch light or dark theme"
        >
          Light / dark
        </button>
        <button type="button" className="rv-btn" onClick={() => print()}>
          Print
        </button>
      </div>
      <nav className="rv-wrap rv-tabs" aria-label="Sections">
        {sections.map((s) => (
          <a
            key={s.id}
            href={`#${s.id}`}
            className={on === s.id ? "rv-on" : undefined}
            aria-current={on === s.id ? "true" : undefined}
          >
            {s.label}
          </a>
        ))}
      </nav>
    </header>
  );
}

export function Section({
  id,
  eyebrow,
  question,
  hint,
  children,
}: {
  id: string;
  eyebrow: string;
  question: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <section className="rv-s" id={id} aria-labelledby={`${id}-h`}>
      <div className="rv-s-head">
        <div className="rv-eyebrow">{eyebrow}</div>
        <h2 id={`${id}-h`}>{question}</h2>
        {hint && <p>{hint}</p>}
      </div>
      {children}
    </section>
  );
}

export function Takeaway({
  text,
  warn = false,
}: {
  text: string | undefined;
  warn?: boolean;
}) {
  if (!text) return null;
  const cut = text.indexOf(". ");
  return (
    <div className={`rv-takeaway${warn ? " rv-warn" : ""}`}>
      {cut > 0 ? (
        <>
          <b>{text.slice(0, cut + 1)}</b> {text.slice(cut + 2)}
        </>
      ) : (
        text
      )}
    </div>
  );
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <div className="rv-facts">
      {items.map(([k, v]) => (
        <div key={k}>
          <div className="rv-k">{k}</div>
          <div className="rv-v">{v}</div>
        </div>
      ))}
    </div>
  );
}
