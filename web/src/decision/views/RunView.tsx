// The run viewer (DESIGN.md section 4): answer, orders at risk, choke point, best fix,
// details. Data: RunViewModel (DATA-CONTRACT.md section 2).
import { Page, type SectionRef } from "../Page";
import { Card, EmptyState, Section, SectionLink } from "../primitives";
import { ChokeFlow } from "../run/ChokeFlow";
import { LeverList } from "../run/LeverList";
import { OrderRiskList } from "../run/OrderRiskList";
import { RunDetails } from "../run/RunDetails";
import "../run/run.css";
import { AnswerHero } from "../AnswerHero";
import { headlineParts, heroFigures, sideRows } from "../run";
import type { RunViewModel } from "../types";

const SECTIONS: SectionRef[] = [
  { id: "answer", label: "Answer" },
  { id: "orders", label: "Orders at risk" },
  { id: "choke", label: "Choke point" },
  { id: "fix", label: "Best fix" },
  { id: "details", label: "Details" },
];

/** "The shaft mill is the choke point. It is busy 99%." with the first sentence bold. */
function Lead({ text }: { text: string }) {
  const cut = text.indexOf(". ");
  if (cut < 0) return <>{text}</>;
  return (
    <>
      <b>{text.slice(0, cut + 1)}</b> {text.slice(cut + 2)}
    </>
  );
}

export function RunView({ model }: { model: RunViewModel }) {
  const { about, answer } = model;
  const hasDue = model.orders.some((o) => o.due != null);
  const tested = model.levers.some((l) => l.tested);
  const runs = model.sweep?.reps ?? about.reps;
  const orders = model.takeaways?.orders;
  const choke = model.takeaways?.choke;
  return (
    <Page
      view="run"
      title="Run viewer"
      project={about.project}
      scope={`${model.orders.length} orders · ${about.reps} test ${about.reps === 1 ? "run" : "runs"} · times in ${about.time_unit}`}
      sections={SECTIONS}
      footer={
        <>
          twinflow run viewer · {about.project} · run {about.run_id}
        </>
      }
    >
      <RunAnswer model={model} />
      <div className="tf-wrap">
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
          {hasDue ? (
            <Card takeaway={orders ? <Lead text={orders} /> : undefined}>
              <OrderRiskList
                orders={model.orders}
                horizon={about.horizon}
                unit={about.time_unit}
              />
            </Card>
          ) : (
            <EmptyState
              title="No due dates in this plan, so nothing can be late."
              body={
                <p>
                  Add <code>due_date</code> to see risk.
                </p>
              }
            />
          )}
        </Section>
        <Section
          id="choke"
          eyebrow="Choke point"
          question="Where does the line choke?"
          hint="How busy each step is. A step near 100% has no room left. Work piles up in front of it."
        >
          <Card
            takeaway={choke ? <Lead text={choke} /> : undefined}
            tone={answer.choke ? "warn" : "info"}
          >
            <ChokeFlow
              routes={model.routes}
              centres={model.centres}
              label={choke ?? "How busy each step is, by part."}
            />
          </Card>
        </Section>
        <Section
          id="fix"
          eyebrow="Best fix"
          question="What one change would help most?"
          hint={
            tested
              ? `Each change was tested on the same ${runs} runs as today's plan.`
              : "No change has been tested on this run yet."
          }
        >
          <Card>
            <LeverList levers={model.levers} base={answer.on_time.mean} />
          </Card>
        </Section>
        <RunDetails model={model} />
      </div>
    </Page>
  );
}

/** The run viewer's answer (DESIGN.md 4.1) on the one AnswerHero. */
function RunAnswer({ model }: { model: RunViewModel }) {
  const { answer } = model;
  const [lead, accent] = headlineParts(answer.headline);
  const rows = sideRows(model);
  const tested = model.levers.some((l) => l.tested);
  return (
    <AnswerHero
      id="answer"
      eyebrow="Will my orders finish on time?"
      headline={
        <>
          {lead} {accent && <em>{accent}</em>}
        </>
      }
      lede={answer.lede}
      figures={heroFigures(model).map((f, i) => ({ ...f, small: i > 0 }))}
      figuresLayout="three"
      actions={
        <>
          <SectionLink to="orders" className="tf-btn tf-primary">
            See orders at risk
          </SectionLink>
          <SectionLink to="fix" className="tf-btn">
            {tested ? "See the best fix" : "See what to test"}
          </SectionLink>
        </>
      }
      side={
        <>
          <h3>The one change that helps most</h3>
          {rows.map((r) => (
            <div className="tf-floor-row" key={r.key}>
              <div>
                <div className="tf-k">{r.key}</div>
                <div className="tf-ft">
                  {r.from && <s>{r.from}</s>} {r.value}
                </div>
              </div>
              {r.delta && (
                <div className={"tf-d" + (r.warn ? " tf-warn" : "")}>
                  {r.delta}
                </div>
              )}
              {r.why && <div className="tf-why">{r.why}</div>}
              {r.command && <code className="rv-cmd">{r.command}</code>}
            </div>
          ))}
        </>
      }
    >
      {answer.command && (
        <code className="rv-cmd rv-cmd-hero">{answer.command}</code>
      )}
    </AnswerHero>
  );
}
