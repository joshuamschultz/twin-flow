// The standard page: top bar, sticky section bar with scroll spy, content, footer
// (DESIGN.md 0.1). Same in the workspace and the offline file.
import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { scrollToSection, SectionLink } from "./primitives";
import { TooltipHost } from "./Tooltip";
import {
  readParams,
  setViewParam,
  ShellContext,
  type Shell,
} from "./viewState";

export type SectionRef = { id: string; label: string };

/** Buttons a host shell adds to every page top bar (the workspace adds Export HTML). */
export const PageActionsContext = createContext<ReactNode>(null);

function useActiveSection(sections: SectionRef[]): string {
  const [active, setActive] = useState(sections[0]?.id ?? "");
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setActive(e.target.id);
      },
      { rootMargin: "-110px 0px -65% 0px" },
    );
    for (const s of sections) {
      const el = document.getElementById(s.id);
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, [sections]);
  return active;
}

function currentTheme(): "light" | "dark" {
  const set = document.documentElement.dataset.theme;
  if (set === "light" || set === "dark") return set;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

/** Applies `theme=light|dark` from the URL, for links and screenshots, now and when
 *  the hash changes. */
export function applyThemeFromUrl(): void {
  readThemeParam();
  window.addEventListener("hashchange", readThemeParam);
}

function readThemeParam(): void {
  const wanted =
    readParams(window.location.hash).theme ??
    readParams(window.location.search).theme;
  if (wanted === "light" || wanted === "dark")
    document.documentElement.dataset.theme = wanted;
}

function ThemeToggle({ shell }: { shell: Shell }) {
  const [, redraw] = useState(0);
  const flip = () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    if (shell === "offline") setViewParam(shell, "theme", next);
    redraw((n) => n + 1);
  };
  return (
    <button
      type="button"
      className="tf-btn"
      onClick={flip}
      aria-label="Switch light or dark theme"
    >
      Light / dark
    </button>
  );
}

export function Page(props: {
  view: string;
  title: string;
  project: string;
  scope: string;
  sections: SectionRef[];
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  const shell = useContext(ShellContext);
  const hostActions = useContext(PageActionsContext);
  const active = useActiveSection(props.sections);
  useEffect(() => {
    const tab = readParams(
      shell === "offline" ? window.location.hash : window.location.search,
    ).tab;
    if (tab && tab !== props.sections[0]?.id) scrollToSection(tab);
  }, [shell, props.sections]);
  return (
    <TooltipHost>
      <div className="tf-root" data-view={props.view}>
        <header className="tf-top">
          <div className="tf-wrap tf-top-row">
            {shell === "offline" && (
              <span className="tf-brand">
                twinflow<i>.</i>
              </span>
            )}
            <span className="tf-crumb">
              <b>{props.title}</b> · {props.project}{" "}
              <span className="tf-opt">· {props.scope}</span>
            </span>
            <span className="tf-spacer" />
            {props.actions}
            {hostActions}
            <ThemeToggle shell={shell} />
            <button
              type="button"
              className="tf-btn"
              onClick={() => window.print()}
            >
              Print
            </button>
          </div>
          <nav className="tf-wrap tf-tabs" aria-label="Sections">
            {props.sections.map((s) => (
              <SectionLink key={s.id} to={s.id} current={s.id === active}>
                {s.label}
              </SectionLink>
            ))}
          </nav>
        </header>
        <main className="tf-main">{props.children}</main>
        {props.footer && (
          <footer className="tf-wrap tf-footer">{props.footer}</footer>
        )}
      </div>
    </TooltipHost>
  );
}
