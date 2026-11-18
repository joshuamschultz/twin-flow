// The sweep view (BUILD-PLAN.md B-35): "What one change would help most?" for a
// `twinflow balance` or `twinflow optimize` folder. Built from the shared parts.
import { Page, type SectionRef } from "../Page";
import { Card, Section } from "../primitives";
import { headlineParts, pct } from "../run";
import { LeverList } from "../run/LeverList";
import "../run/run.css";
import { AnswerHero } from "../AnswerHero";
import type { RunLever, SweepViewModel } from "../types";

const SECTIONS: SectionRef[] = [
  { id: "answer", label: "Answer" },
  { id: "fix", label: "Changes" },
  { id: "details", label: "Details" },
];

export function SweepView({ model }: { model: SweepViewModel }) {
  const { about, answer } = model;
  const [lead, accent] = headlineParts(answer.headline);
  const today = answer.today.on_time;
  const best = answer.best;
  const kind = about.kind === "optimize" ? "Search" : "Sweep";
  const f = (v: number | null | undefined, d = 1) =>
    v == null ? "-" : v.toFixed(d);
  return (
    <Page
      view="sweep"
      title={kind}
      project={about.project}
      scope={`${model.points.length} points · ${about.reps} test runs each`}
      sections={SECTIONS}
      footer={
        <>
          twinflow {about.kind} · {about.project} · {about.sweep_id}
        </>
      }
    >
      <AnswerHero
        id="answer"
        eyebrow="What one change would help most?"
        headline={
          <>
            {lead} {accent && <em>{accent}</em>}
          </>
        }
        lede={answer.lede}
        figures={[
          ...(today != null
            ? [{ value: pct(today), label: "of orders on time today" }]
            : []),
          ...(best?.on_time != null
            ? [
                {
                  value: pct(best.on_time),
                  label: "on time with the best change",
                  small: true,
                },
              ]
            : []),
          ...(best?.late_orders_per_run != null
            ? [
                {
                  value: f(best.late_orders_per_run),
                  label: "late orders in a normal run",
                  small: true,
                },
              ]
            : []),
        ]}
        figuresLayout="three"
        side={
          <>
            <h3>What was tested</h3>
            <div className="tf-floor-row">
              <div>
                <div className="tf-k">Changes</div>
                <div className="tf-ft">{model.levers.length}</div>
              </div>
              <div className="tf-why">
                Each one on the same {about.reps} test runs as today&apos;s
                plan.
              </div>
            </div>
            <div className="tf-floor-row">
              <div>
                <div className="tf-k">Base run</div>
                <div className="tf-ft">
                  {about.base_run_id ? "Linked" : "None"}
                </div>
              </div>
              <div className="tf-why">
                {about.base_run_id
                  ? `Its run viewer now lists these changes. Run ${about.base_run_id}.`
                  : "Add --run <run-id> to show these changes in a run viewer."}
              </div>
            </div>
          </>
        }
      />
      <div className="tf-wrap">
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
          <Card>
            <LeverList
              levers={model.levers as RunLever[]}
              base={today ?? null}
            />
          </Card>
        </Section>
        <Section id="details" eyebrow="Details" question="Every point tested">
          <Card>
            <div
              className="tf-scroll"
              tabIndex={0}
              role="region"
              aria-label="Every point tested"
            >
              <table className="tf-table" aria-label="Every point tested">
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
            </div>
            <p className="tf-note">
              {about.method}
              {about.base_run_id ? ` Base run: ${about.base_run_id}.` : ""}
            </p>
          </Card>
        </Section>
      </div>
    </Page>
  );
}
