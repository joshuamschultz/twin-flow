// The race view (DESIGN.md section 3). This build has the decision, the cautions, why it
// wins and all rules. The explore sections come in the next builds (BUILD-PLAN phase C).
import { CautionCards } from "../CautionCards";
import { GapLine } from "../GapLine";
import { DecisionHero } from "../hero";
import { Leaderboard } from "../Leaderboard";
import { ReplayLanes } from "../ReplayLanes";
import { replayRuns } from "../replay";
import { Page, type SectionRef } from "../Page";
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

const SECTIONS: SectionRef[] = [
  { id: "decision", label: "Decision" },
  { id: "why", label: "Why it wins" },
  { id: "rules", label: "All rules" },
  { id: "details", label: "Details" },
];
const NO_COMPARE: SectionRef[] = [
  { id: "decision", label: "Decision" },
  { id: "rules", label: "All rules" },
  { id: "details", label: "Details" },
];

const COMING = [
  "Compare two rules side by side",
  "What if my costs are different",
  "State map",
  "Money leaks",
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
  const replay = replayRuns(model);
  const base = compared ? SECTIONS : NO_COMPARE;
  const sections = replay.length
    ? [
        ...base.slice(0, -1),
        { id: "replay", label: "Floor replay" },
        ...base.slice(-1),
      ]
    : base;
  return (
    <Page
      view="race"
      title="Policy race"
      project={a.project}
      scope={`${a.episodes} runs of ${a.horizon_days} days · same customer orders for every rule`}
      sections={sections}
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
        {replay.length > 0 && (
          <Section
            id="replay"
            eyebrow="Floor replay"
            question={`What does a normal ${a.horizon_days} days look like on the floor?`}
            hint="One typical run, same customer orders for both rules. Drag the day, or click a chart."
          >
            <Card>
              <ReplayLanes
                runs={replay}
                products={model.products}
                families={model.families}
                economics={model.economics}
              />
            </Card>
          </Section>
        )}
        <Section id="details" eyebrow="Details" question="The full numbers">
          <EmptyState
            title="More of this report is on the way."
            body={
              <>
                <p>
                  This build shows the decision, the cautions, why it wins and
                  all rules. The next builds add:
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
