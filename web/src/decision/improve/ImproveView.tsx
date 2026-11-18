// The improve view (DESIGN.md 6, DATA-CONTRACT 4): did the new rule win, and can I trust it?
import { FloorChanges } from "../hero";
import { signedUsd, usd } from "../format";
import { Page, type SectionRef } from "../Page";
import {
  Card,
  EmptyState,
  Section,
  SectionLink,
  StatusPill,
} from "../primitives";
import { RunDots } from "../RunDots";
import type { ImproveViewModel } from "../types";
import { AnswerHero, Problems, share } from "../answer";
import { checkSummary, rangeWords } from "./derive";

const SECTIONS: SectionRef[] = [
  { id: "answer", label: "Answer" },
  { id: "proof", label: "Proof" },
  { id: "details", label: "Details" },
];

export function ImproveView({ model }: { model: ImproveViewModel }) {
  const {
    outcome,
    candidate,
    previous_best: previous,
    confidence,
    about,
  } = model;
  const won = outcome.level === "won";
  const gaps = confidence?.gaps ?? [];
  return (
    <Page
      view="improve"
      title="Improve"
      project={about.project}
      scope={`${candidate.label} against ${previous.label}`}
      sections={SECTIONS}
      footer={
        <>
          twinflow improve · {about.project} · {about.episodes} runs, seed{" "}
          {about.seed}
        </>
      }
    >
      <AnswerHero
        eyebrow="Did the new rule win?"
        headline={outcome.headline}
        lede={outcome.reason}
        badge={
          confidence
            ? {
                level: confidence.level,
                words: confidence.words,
                detail: confidence.detail,
              }
            : undefined
        }
        aside={<FloorChanges rows={model.floor} />}
      >
        {gaps.length > 1 && confidence && (
          <div style={{ marginTop: 14 }}>
            <RunDots
              gaps={gaps}
              mean={confidence.mean ?? 0}
              low={confidence.low}
              high={confidence.high}
            />
          </div>
        )}
        <div className="tf-hero-actions">
          <a className="tf-btn tf-primary" href={model.race_ref}>
            Open the full race
          </a>
          <SectionLink to="proof" className="tf-btn">
            See the proof
          </SectionLink>
        </div>
        {won && outcome.copied_to && (
          <p className="tf-hero-note">
            {candidate.label} was copied to {outcome.copied_to}.
          </p>
        )}
        {!won && (
          <p className="tf-hero-note">
            Nothing was copied. {previous.label} stays your best rule.
          </p>
        )}
      </AnswerHero>
      <div className="tf-wrap">
        <Section
          id="proof"
          eyebrow="Proof"
          question="Can you trust this result?"
          hint={checkSummary(model.checks)}
        >
          <div className="tf-grid2">
            <Card
              question="Proof checklist"
              sub="Each line is a pass or a fail."
            >
              <ul className="tf-list" aria-label="Proof checklist">
                {model.checks.map((c) => (
                  <li key={c.key}>
                    <div className="tf-row">
                      <span>{c.label}</span>
                      <StatusPill
                        kind={c.pass ? "ok" : "late"}
                        label={c.pass ? "Pass" : "Fail"}
                      />
                    </div>
                    <p className="tf-note">{c.detail}</p>
                  </li>
                ))}
              </ul>
            </Card>
            <Card
              question="How the two rules did"
              sub="Dollars per test run."
              takeaway={`The gain was ${signedUsd(model.versus.dollars?.mean ?? 0)} per run. The likely range is ${rangeWords(model.versus.dollars?.low ?? null, model.versus.dollars?.high ?? null)}.`}
            >
              <table className="tf-table">
                <thead>
                  <tr>
                    <th scope="col">Rule</th>
                    <th scope="col">Rank</th>
                    <th scope="col">Dollars per run</th>
                  </tr>
                </thead>
                <tbody>
                  {[candidate, previous].map((r, i) => (
                    <tr key={r.policy}>
                      <td>
                        {r.label}{" "}
                        <span className="tf-note">
                          {i === 0 ? "new" : "old best"}
                        </span>
                      </td>
                      <td className="tf-num">{r.rank}</td>
                      <td className="tf-num">{usd(r.dollars.mean)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          </div>
        </Section>
        <Section id="details" eyebrow="Details" question="How this was made">
          <div className="tf-grid2">
            <Card question="Run facts">
              <dl className="tf-kv">
                <dt>Test runs</dt>
                <dd className="tf-num">{about.episodes}</dd>
                <dt>Seed</dt>
                <dd className="tf-num">{about.seed}</dd>
                <dt>Likely range</dt>
                <dd className="tf-num">{share(about.interval)}</dd>
                <dt>New rule file</dt>
                <dd>{candidate.file}</dd>
                <dt>File fingerprint</dt>
                <dd className="tf-num" style={{ wordBreak: "break-all" }}>
                  {candidate.sha256}
                </dd>
              </dl>
            </Card>
            <Card question="Problems">
              {model.problems.length ? (
                <Problems problems={model.problems} />
              ) : (
                <EmptyState title="No problems." />
              )}
            </Card>
          </div>
        </Section>
      </div>
    </Page>
  );
}
