// The sweep view (BUILD-PLAN.md B-35): "What one change would help most?" for a
// `twinflow balance` or `twinflow optimize` folder. Reuses the run viewer's parts.
import { useEffect } from "react";
import type { RunLever, SweepViewModel } from "../types";
import { LeverList } from "./LeverList";
import {
  Facts,
  initialTheme,
  Section,
  TopBar,
  type SectionLink,
} from "./parts";
import { headlineParts, pct } from "./runText";
import "./run.css";

export function SweepView({ model }: { model: SweepViewModel }) {
  useEffect(initialTheme, []);
  const { about, answer } = model;
  const [lead, accent] = headlineParts(answer.headline);
  const today = answer.today.on_time;
  const best = answer.best;
  const sections: SectionLink[] = [
    { id: "answer", label: "Answer" },
    { id: "fix", label: "Changes" },
    { id: "details", label: "Details" },
  ];
  const f = (v: number | null | undefined, d = 1) =>
    v == null ? "-" : v.toFixed(d);
  return (
    <div className="rv-root" data-view="sweep">
      <TopBar
        view={about.kind === "optimize" ? "Search" : "Sweep"}
        scope={about.project}
        detail={`${model.points.length} points · ${about.reps} test runs each`}
        sections={sections}
      />
      <main>
        <div className="rv-hero" id="answer">
          <div className="rv-wrap rv-hero-grid">
            <div>
              <div className="rv-eyebrow">What one change would help most?</div>
              <h1>
                {lead} {accent && <em>{accent}</em>}
              </h1>
              <p className="rv-lede">{answer.lede}</p>
              <div className="rv-money-row">
                {today != null && (
                  <div className="rv-money">
                    <div className="rv-v">{pct(today)}</div>
                    <div className="rv-k">of orders on time today</div>
                  </div>
                )}
                {best?.on_time != null && (
                  <div className="rv-money">
                    <div className="rv-v rv-small">{pct(best.on_time)}</div>
                    <div className="rv-k">on time with the best change</div>
                  </div>
                )}
                {best?.late_orders_per_run != null && (
                  <div className="rv-money">
                    <div className="rv-v rv-small">
                      {f(best.late_orders_per_run)}
                    </div>
                    <div className="rv-k">late orders in a normal run</div>
                  </div>
                )}
              </div>
            </div>
            <div className="rv-floor">
              <h3>What was tested</h3>
              <div className="rv-floor-row">
                <div>
                  <div className="rv-k">Changes</div>
                  <div className="rv-ft">{model.levers.length}</div>
                </div>
                <div className="rv-why">
                  Each one on the same {about.reps} test runs as today&apos;s
                  plan.
                </div>
              </div>
              <div className="rv-floor-row">
                <div>
                  <div className="rv-k">Base run</div>
                  <div className="rv-ft">
                    {about.base_run_id ? "Linked" : "None"}
                  </div>
                </div>
                <div className="rv-why">
                  {about.base_run_id
                    ? `Its run viewer now lists these changes. Run ${about.base_run_id}.`
                    : "Add --run <run-id> to show these changes in a run viewer."}
                </div>
              </div>
            </div>
          </div>
        </div>
        <div className="rv-wrap">
          {model.warnings.map((w) => (
            <div className="rv-banner" role="status" key={w}>
              {w}
            </div>
          ))}
          <Section
            id="fix"
            eyebrow="Changes"
            question="What one change would help most?"
            hint="Best first. The bar runs from today's on-time share to the new one."
          >
            <div className="rv-card">
              <LeverList
                levers={model.levers as RunLever[]}
                base={today ?? null}
              />
            </div>
          </Section>
          <Section id="details" eyebrow="Details" question="Every point tested">
            <Facts
              items={[
                ["Points", model.points.length],
                ["Test runs each", about.reps],
                ["Kind", about.kind === "optimize" ? "Search" : "Sweep"],
                ["Base run", about.base_run_id ?? "-"],
              ]}
            />
            <div className="rv-card rv-scroll">
              <table className="rv-table" aria-label="Every point tested">
                <thead>
                  <tr>
                    <th>Change</th>
                    <th>Settings</th>
                    <th>Test runs</th>
                    <th>On time</th>
                    <th>Late orders per run</th>
                    <th>Machine busy hours</th>
                  </tr>
                </thead>
                <tbody>
                  {model.points.map((p) => (
                    <tr key={p.id}>
                      <td>{p.today ? "What you do today" : p.label}</td>
                      <td>
                        {Object.entries(p.levers)
                          .map(([k, v]) => `${k}=${String(v)}`)
                          .join(", ")}
                      </td>
                      <td>{p.reps}</td>
                      <td>{pct(p.on_time, 1)}</td>
                      <td>{f(p.late_orders_per_run, 2)}</td>
                      <td>{f(p.busy_hours)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="rv-note">{about.method}</p>
            </div>
          </Section>
        </div>
      </main>
      <footer className="rv-wrap rv-footer">
        twinflow {about.kind} · {about.project} · {about.sweep_id}
      </footer>
    </div>
  );
}
