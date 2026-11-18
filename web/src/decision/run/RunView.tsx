// The run viewer (DESIGN.md section 4): answer, orders at risk, choke point, best fix,
// details. Data: RunViewModel (DATA-CONTRACT.md section 2).
import { useEffect, useState, type ReactNode } from "react";
import type { RunViewModel } from "../types";
import { ChokeFlow } from "./ChokeFlow";
import { LeverList } from "./LeverList";
import { OrderRiskList, OrderTable } from "./OrderRiskList";
import {
  displayId,
  headlineParts,
  heroFigures,
  pct,
  sideRows,
  unitShort,
} from "./runText";
import "./run.css";

type SectionLink = { id: string; label: string };

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

function initialTheme(): void {
  const q =
    new URLSearchParams(location.search).get("theme") ??
    new URLSearchParams(location.hash.slice(1)).get("theme");
  if (q === "dark" || q === "light") document.documentElement.dataset.theme = q;
}

function TopBar({
  model,
  sections,
}: {
  model: RunViewModel;
  sections: SectionLink[];
}) {
  const on = useScrollSpy(sections.map((s) => s.id));
  const { about } = model;
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
          <b>Run viewer</b> · {about.project}{" "}
          <span className="rv-opt">
            · {model.orders.length} orders · {about.reps} test{" "}
            {about.reps === 1 ? "run" : "runs"} · times in {about.time_unit}
          </span>
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

function Hero({ model }: { model: RunViewModel }) {
  const { answer } = model;
  const [lead, accent] = headlineParts(answer.headline);
  const figures = heroFigures(model);
  const rows = sideRows(model);
  const best = model.levers.some((l) => l.tested);
  return (
    <div className="rv-hero" id="answer">
      <div className="rv-wrap rv-hero-grid">
        <div>
          <div className="rv-eyebrow">Will my orders finish on time?</div>
          <h1>
            {lead} {accent && <em>{accent}</em>}
          </h1>
          <p className="rv-lede">{answer.lede}</p>
          {answer.command && (
            <code className="rv-cmd rv-cmd-hero">{answer.command}</code>
          )}
          <div className="rv-money-row">
            {figures.map((f, i) => (
              <div className="rv-money" key={f.label}>
                <div className={`rv-v${i ? " rv-small" : ""}`}>{f.value}</div>
                <div className="rv-k">{f.label}</div>
              </div>
            ))}
          </div>
          <div className="rv-hero-actions">
            <a className="rv-btn rv-primary" href="#orders">
              See orders at risk
            </a>
            <a className="rv-btn" href="#fix">
              {best ? "See the best fix" : "See what to test"}
            </a>
          </div>
        </div>
        <div className="rv-floor">
          <h3>The one change that helps most</h3>
          {rows.map((r) => (
            <div className="rv-floor-row" key={r.key}>
              <div>
                <div className="rv-k">{r.key}</div>
                <div className="rv-ft">
                  {r.from && <s>{r.from}</s>} {r.value}
                </div>
              </div>
              {r.delta && (
                <div className={`rv-d${r.warn ? " rv-warn" : ""}`}>
                  {r.delta}
                </div>
              )}
              {r.why && <div className="rv-why">{r.why}</div>}
              {r.command && <code className="rv-cmd">{r.command}</code>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Section({
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

function Takeaway({
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

function Facts({ items }: { items: [string, ReactNode][] }) {
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

function WipLine({ model }: { model: RunViewModel }) {
  const wip = model.wip_by_time;
  if (!wip || wip.t.length < 2) return null;
  const series = Object.entries(wip.by_centre).filter(([, v]) =>
    v.some((x) => x > 0),
  );
  const W = 640;
  const H = 160;
  const tMax = Math.max(...wip.t, 1e-9);
  const yMax = Math.max(1, ...series.flatMap(([, v]) => v));
  const label = (id: string) =>
    model.centres.find((c) => c.id === id)?.label ?? id;
  const path = (v: number[]) =>
    v
      .map((y, i) => {
        const x = (wip.t[i] / tMax) * W;
        const yy = H - (y / yMax) * (H - 10);
        return i === 0 ? `M${x},${yy}` : `H${x}V${yy}`;
      })
      .join("");
  return (
    <div className="rv-card">
      <h3>How much work waits at each step over time?</h3>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`Jobs at each step over the first test run. Peak ${yMax}.`}
        className="rv-wip"
        preserveAspectRatio="none"
      >
        {series.map(([id, v], i) => (
          <path key={id} d={path(v)} className={`rv-wip-line rv-wip-${i % 5}`}>
            <title>{label(id)}</title>
          </path>
        ))}
      </svg>
      <div className="rv-legend">
        {series.map(([id], i) => (
          <span key={id}>
            <i className={`rv-wip-key rv-wip-${i % 5}`} />
            {label(id)}
          </span>
        ))}
      </div>
      <p className="rv-note">
        First test run. A rising line is a growing queue. Time in{" "}
        {model.about.time_unit}.
      </p>
    </div>
  );
}

function Details({ model }: { model: RunViewModel }) {
  const { about, details } = model;
  const u = unitShort(about.time_unit);
  const f = (v: number | null | undefined, d = 1) =>
    v == null ? "-" : v.toFixed(d);
  return (
    <Section id="details" eyebrow="Details" question="The full numbers">
      <Facts
        items={[
          ["Test runs", about.reps],
          ["Orders", model.orders.length],
          ["Range shown", pct(about.interval, 0)],
          ["Random seed", about.seed],
          ["Run length", `${f(about.horizon)} ${u}`],
          [
            "Orders done, first run",
            details?.completed != null
              ? `${details.completed} of ${model.orders.length}`
              : "-",
          ],
          ["Machine hours, first run", f(details?.machine_hours, 2)],
          ["Setup hours, first run", f(details?.setup_hours, 2)],
        ]}
      />
      <div className="rv-card rv-scroll">
        <h3>Every order</h3>
        <OrderTable orders={model.orders} unit={about.time_unit} />
        <p className="rv-note">
          Times in {about.time_unit} from the start of the run
          {about.start ? ` (${about.start.replace("T", " ")})` : ""}. Room is
          the due time minus the likely finish.
        </p>
      </div>
      <div className="rv-card rv-scroll">
        <h3>Every work centre</h3>
        <table className="rv-table" aria-label="Every work centre in numbers">
          <thead>
            <tr>
              <th>Work centre</th>
              <th>Id</th>
              <th>Busy</th>
              <th>Likely range</th>
              <th>Wait in line, per job</th>
              <th>Idle, no work</th>
              <th>Held after done</th>
            </tr>
          </thead>
          <tbody>
            {model.centres.map((c) => (
              <tr key={c.id}>
                <td>{c.label}</td>
                <td>{c.id}</td>
                <td>{pct(c.busy.mean ?? 0)}</td>
                <td>
                  {c.busy.low != null && c.busy.high != null
                    ? `${pct(c.busy.low)} to ${pct(c.busy.high)}`
                    : "-"}
                </td>
                <td>
                  {f(c.queue_wait)} {u}
                </td>
                <td>
                  {f(c.starved)} {u}
                </td>
                <td>
                  {f(c.blocked)} {u}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {details?.centres && details.centres.length > 0 && (
        <div className="rv-card rv-scroll">
          <h3>First test run, by work centre</h3>
          <table
            className="rv-table"
            aria-label="First test run by work centre"
          >
            <thead>
              <tr>
                <th>Work centre</th>
                <th>Id</th>
                <th>Busy</th>
                <th>Jobs</th>
                <th>Time per job</th>
                <th>Most jobs at once</th>
                <th>Idle, no work</th>
                <th>Held after done</th>
              </tr>
            </thead>
            <tbody>
              {details.centres.map((c) => (
                <tr key={c.id}>
                  <td>{c.label}</td>
                  <td>{c.id}</td>
                  <td>{pct(c.busy)}</td>
                  <td>{c.jobs}</td>
                  <td>
                    {f(c.cycle, 2)} {u}
                  </td>
                  <td>{c.wip_peak}</td>
                  <td>
                    {f(c.starved)} {u}
                  </td>
                  <td>
                    {f(c.blocked)} {u}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {details?.orders && details.orders.length > 0 && (
        <div className="rv-card rv-scroll">
          <h3>First test run, by order</h3>
          <table className="rv-table" aria-label="First test run by order">
            <thead>
              <tr>
                <th>Order</th>
                <th>Done at</th>
                <th>Late by</th>
              </tr>
            </thead>
            <tbody>
              {details.orders.map((o) => (
                <tr key={o.id}>
                  <td>{displayId(o.id)}</td>
                  <td>
                    {f(o.finish)} {o.finish == null ? "" : u}
                  </td>
                  <td>
                    {o.late_by == null
                      ? "-"
                      : `${o.late_by > 0 ? "+" : o.late_by < 0 ? "−" : ""}${Math.abs(o.late_by).toFixed(1)} ${u}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="rv-note">A minus sign means early.</p>
        </div>
      )}
      <div className="rv-grid2">
        {details?.labor_pools && details.labor_pools.length > 0 && (
          <div className="rv-card rv-scroll">
            <h3>People</h3>
            <table className="rv-table" aria-label="Labor pools">
              <thead>
                <tr>
                  <th>Pool</th>
                  <th>Skills</th>
                  <th>People</th>
                  <th>Busy on shift</th>
                  <th>Busy, all hours</th>
                  <th>Busy hours</th>
                </tr>
              </thead>
              <tbody>
                {details.labor_pools.map((r) => (
                  <tr key={r.resource}>
                    <td>{r.resource}</td>
                    <td>{r.skills.join(", ")}</td>
                    <td>{r.capacity}</td>
                    <td>{pct(r.busy_on_shift)}</td>
                    <td>{pct(r.busy)}</td>
                    <td>{f(r.busy_hours, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {model.labor && model.labor.length > 0 && (
              <table className="rv-table" aria-label="Labor hours by skill">
                <thead>
                  <tr>
                    <th>Pool</th>
                    <th>Skill</th>
                    <th>Hours</th>
                  </tr>
                </thead>
                <tbody>
                  {model.labor.map((r) => (
                    <tr key={`${r.pool}-${r.skill}`}>
                      <td>{r.pool}</td>
                      <td>{r.skill}</td>
                      <td>{f(r.hours, 3)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
        {details?.machine_pools && details.machine_pools.length > 0 && (
          <div className="rv-card rv-scroll">
            <h3>Machines</h3>
            <table className="rv-table" aria-label="Machine pools">
              <thead>
                <tr>
                  <th>Machine</th>
                  <th>Steps</th>
                  <th>Slots</th>
                  <th>Busy</th>
                  <th>Hours</th>
                </tr>
              </thead>
              <tbody>
                {details.machine_pools.map((r) => {
                  const hours = details.machine_hours_by_machine?.find(
                    (m) => m.machine === r.resource,
                  );
                  return (
                    <tr key={r.resource}>
                      <td>{r.resource}</td>
                      <td>{r.steps.join(", ")}</td>
                      <td>{r.capacity}</td>
                      <td>{pct(r.busy)}</td>
                      <td>{f(hours?.hours ?? r.busy_hours, 3)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <WipLine model={model} />
      <div className="rv-card">
        <h3>How this was worked out</h3>
        <p className="rv-note">{about.method}</p>
        {about.assumptions.length > 0 ? (
          <>
            <p className="rv-note">
              The model left these out, so twinflow used a default:
            </p>
            <ul className="rv-list">
              {about.assumptions.map((a) => (
                <li key={a}>{a}</li>
              ))}
            </ul>
          </>
        ) : (
          <p className="rv-note">
            No defaults were used. Every value came from the model.
          </p>
        )}
        {model.problems.length > 0 && (
          <ul className="rv-list">
            {model.problems.map((p) => (
              <li key={p.title}>
                <b>{p.title}.</b> {p.body}
              </li>
            ))}
          </ul>
        )}
        <p className="rv-note">
          Run {about.run_id}
          {about.engine_version ? ` · twinflow ${about.engine_version}` : ""}
          {about.model_hash ? ` · model ${about.model_hash.slice(0, 12)}` : ""}
          {about.plan_hash ? ` · plan ${about.plan_hash.slice(0, 12)}` : ""}
        </p>
      </div>
    </Section>
  );
}

export function RunView({ model }: { model: RunViewModel }) {
  useEffect(initialTheme, []);
  const { about, answer } = model;
  const hasDue = model.orders.some((o) => o.due != null);
  const sections: SectionLink[] = [
    { id: "answer", label: "Answer" },
    { id: "orders", label: "Orders at risk" },
    { id: "choke", label: "Choke point" },
    { id: "fix", label: "Best fix" },
    { id: "details", label: "Details" },
  ];
  const runs = model.sweep?.reps ?? about.reps;
  return (
    <div className="rv-root" data-view="run">
      <TopBar model={model} sections={sections} />
      <main>
        <Hero model={model} />
        <div className="rv-wrap">
          {model.warnings.map((w) => (
            <div className="rv-banner" role="status" key={w}>
              {w}
            </div>
          ))}
          <Section
            id="orders"
            eyebrow="Orders at risk"
            question="Which orders are at risk, and why?"
            hint={
              hasDue
                ? "Sorted by the chance of being late. The bar runs from release to the likely finish. The tick is the due time. Press an order to see where its time goes."
                : undefined
            }
          >
            <div className="rv-card">
              {hasDue ? (
                <>
                  <OrderRiskList
                    orders={model.orders}
                    horizon={about.horizon}
                    unit={about.time_unit}
                  />
                  <Takeaway text={model.takeaways?.orders} />
                </>
              ) : (
                <div className="rv-empty">
                  <b>No due dates in this plan, so nothing can be late.</b> Add{" "}
                  <code>due_date</code> to see risk.
                </div>
              )}
            </div>
          </Section>
          <Section
            id="choke"
            eyebrow="Choke point"
            question="Where does the line choke?"
            hint="How busy each step is. A step near 100% has no room left. Work piles up in front of it."
          >
            <div className="rv-card">
              <ChokeFlow routes={model.routes} centres={model.centres} />
              <Takeaway
                text={model.takeaways?.choke}
                warn={answer.choke != null}
              />
            </div>
          </Section>
          <Section
            id="fix"
            eyebrow="Best fix"
            question="What one change would help most?"
            hint={
              model.levers.some((l) => l.tested)
                ? `Each change was tested on the same ${runs} runs as today's plan.`
                : "No change has been tested on this run yet."
            }
          >
            <div className="rv-card">
              <LeverList levers={model.levers} base={answer.on_time.mean} />
            </div>
          </Section>
          <Details model={model} />
        </div>
      </main>
      <footer className="rv-wrap rv-footer">
        twinflow run viewer · {about.project} · run {about.run_id}
      </footer>
    </div>
  );
}
