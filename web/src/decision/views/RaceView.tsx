// The race view (DESIGN.md section 3). This build has the decision, the cautions, why it
// wins and all rules. The explore sections come in the next builds (BUILD-PLAN phase C).
import { CautionCards } from "../CautionCards";
import { CompareTable } from "../CompareTable";
import { GapLine } from "../GapLine";
import { DecisionHero } from "../hero";
import { Leaderboard } from "../Leaderboard";
import { Page, type SectionRef } from "../Page";
import { STATE_SECTIONS, StateSections } from "../StateSections";
import { Card, EmptyState, Section, SectionLink } from "../primitives";
import {
  beatToday,
  gapSeries,
  leaderboard,
  pullAheadTakeaway,
  waterfall,
} from "../race";
import type { RaceViewModel } from "../types";
import { Waterfall } from "../Waterfall";
import { WhatIf } from "../WhatIfPanel";

const SECTIONS: SectionRef[] = [
  { id: "decision", label: "Decision" },
  { id: "why", label: "Why it wins" },
  { id: "rules", label: "All rules" },
  { id: "compare", label: "Compare two" },
  { id: "whatif", label: "What if" },
  ...STATE_SECTIONS,
  { id: "details", label: "Details" },
];
const NO_COMPARE: SectionRef[] = [
  { id: "decision", label: "Decision" },
  { id: "rules", label: "All rules" },
  { id: "compare", label: "Compare two" },
  { id: "whatif", label: "What if" },
  ...STATE_SECTIONS,
  { id: "details", label: "Details" },
];

const COMING = [
  "Floor replay, day by day",
  "Tuning",
  "Full tables, method note and downloads",
];

function ProblemsBar({ model }: { model: RaceViewModel }) {
  const n = model.problems?.length ?? 0;
  if (!n) return null;
  return (
    <div className="tf-problems-bar" role="note">
      <div className="tf-wrap">
        {n === 1 ? "1 rule could not run." : `${n} rules could not run.`}{" "}
        <SectionLink to="details">See Details.</SectionLink>
      </div>
    </div>
  );
}

export function RaceView({ model }: { model: RaceViewModel }) {
  const series = gapSeries(model);
  const compared = Boolean(
    model.baseline &&
    model.timing &&
    series.length &&
    !model.recommendation.same_as_baseline,
  );
  const wf = waterfall(model);
  const board = leaderboard(model);
  const a = model.about;
  const runs = model.confidence?.runs ?? a.episodes;
  return (
    <Page
      view="race"
      title="Policy race"
      project={a.project}
      scope={`${a.episodes} runs of ${a.horizon_days} days · same customer orders for every rule`}
      sections={compared ? SECTIONS : NO_COMPARE}
      footer={
        <>
          twinflow policy race · {a.project} · {a.policies_raced} rules,{" "}
          {a.episodes} runs each, seed {a.seed}
          {model.twinflow_version
            ? ` · twinflow ${model.twinflow_version}`
            : ""}
        </>
      }
    >
      <DecisionHero
        model={model}
        actions={
          <>
            {compared && (
              <SectionLink to="why" className="tf-btn tf-primary">
                See why it wins
              </SectionLink>
            )}
            <SectionLink
              to="rules"
              className={"tf-btn" + (compared ? "" : " tf-primary")}
            >
              See all rules
            </SectionLink>
          </>
        }
      />
      <ProblemsBar model={model} />
      {compared && <CautionCards cautions={model.cautions} />}
      <div className="tf-wrap">
        {compared && model.timing && (
          <Section
            id="why"
            eyebrow="Why it wins"
            question="Where does the extra money come from?"
          >
            <div className="tf-grid-a">
              <Card
                question="What adds up to the gain?"
                sub="Dollars per run, pick compared to today. Blue adds profit. Orange takes it away."
                takeaway={model.recommendation.why}
              >
                <Waterfall
                  items={wf.items}
                  total={wf.total}
                  min={wf.min}
                  max={wf.max}
                  totalLabel="Net gain per run"
                  label={model.recommendation.why}
                />
              </Card>
              <Card
                question="When does it pull ahead of today?"
                sub={`Profit so far, minus today's rule, by day. Average of ${runs} runs.`}
                takeaway={pullAheadTakeaway(model)}
              >
                <GapLine
                  series={series}
                  timing={model.timing}
                  label={model.timing.words}
                />
              </Card>
            </div>
          </Section>
        )}
        <Section
          id="rules"
          eyebrow="All rules"
          question="How does every rule compare to today?"
          hint="Profit per run, more or less than today's rule. The thin line is the likely range."
        >
          <Card takeaway={beatToday(model) || undefined}>
            <Leaderboard
              main={board.main}
              rest={board.rest}
              foldLabel={board.foldLabel}
              limit={board.limit}
              label={
                beatToday(model) ||
                "Every rule's profit per run compared with today."
              }
            />
          </Card>
        </Section>
        <Section
          id="compare"
          eyebrow="Compare two"
          question="How do two rules differ, side by side?"
        >
          <CompareTable
            policies={model.policies}
            runs={a.episodes}
            horizonDays={a.horizon_days}
          />
        </Section>
        <Section
          id="whatif"
          eyebrow="What if"
          question="Does the winner change if my costs are different?"
          hint="Move a slider. The ranking updates at once."
        >
          <WhatIf
            policies={model.policies}
            config={model.what_if}
            runs={a.episodes}
          />
        </Section>
        <StateSections model={model} />
        <Section id="details" eyebrow="Details" question="The full numbers">
          <EmptyState
            title="More of this report is on the way."
            body={
              <>
                <p>
                  This build shows the decision, the cautions, why it wins, all
                  rules, compare two, what if, the state map and money leaks. The next builds add:
                </p>
                <ul>
                  {COMING.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
                <p style={{ marginTop: 8 }}>
                  Until then, the full numbers are in race.json next to this
                  file.
                </p>
              </>
            }
          />
        </Section>
      </div>
    </Page>
  );
}
