// The improve view (DESIGN.md 6, DATA-CONTRACT 4): did the new rule win, and can I trust it?
import type { ImproveViewModel } from "../types";
import { Card, Hero, Page, Pill, Problems, Section } from "../viewkit";
import { money, percent, plain, signedMoney } from "../viewkit/format";
import { checkSummary, dotLayout, rangeWords } from "./derive";

const RADIUS = 6;

function RunDots({ model }: { model: ImproveViewModel }) {
  const { gaps, wins, runs } = model.versus;
  const width = 560;
  const { dots, zeroX } = dotLayout(gaps, width, RADIUS);
  const rows = Math.max(1, ...dots.map((d) => d.y / (RADIUS * 2 + 1) + 1));
  const height = rows * (RADIUS * 2 + 1) + 22;
  const label = `${wins} of ${runs} test runs made more money than ${model.previous_best.label}.`;
  return (
    <figure style={{ margin: "22px 0 0" }}>
      <svg
        role="img"
        aria-label={label}
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        style={{ maxWidth: width }}
      >
        <line
          x1={zeroX}
          x2={zeroX}
          y1={0}
          y2={height - 18}
          stroke="#8fa6c4"
          strokeDasharray="3 3"
        />
        {dots.map((d, i) => (
          <circle
            key={i}
            cx={d.x}
            cy={height - 22 - d.y}
            r={RADIUS}
            fill={d.gap >= 0 ? "var(--tf-hero-accent)" : "var(--tf-hero-warn)"}
          />
        ))}
        <text
          x={zeroX}
          y={height - 4}
          textAnchor={zeroX < 90 ? "start" : "middle"}
          fill="#c7d6e8"
          fontSize="11.5"
        >
          no difference
        </text>
      </svg>
      <figcaption style={{ color: "var(--tf-hero-sub)", fontSize: 13.5 }}>
        Each dot is one test run. Blue dots made more than the old best. Orange
        dots made less.
      </figcaption>
    </figure>
  );
}

export function ImproveView({ model }: { model: ImproveViewModel }) {
  const {
    outcome,
    candidate,
    previous_best: previous,
    confidence,
    about,
  } = model;
  const won = outcome.level === "won";
  const sections = [
    { id: "answer", label: "Answer" },
    { id: "proof", label: "Proof" },
    { id: "floor", label: "On the floor" },
    { id: "details", label: "Details" },
  ];
  return (
    <Page
      view="improve"
      viewName="Improve"
      project={about.project}
      scope={`${candidate.label} against ${previous.label}`}
      sections={sections}
    >
      <Hero
        eyebrow="Did the new rule win?"
        headline={outcome.headline}
        lede={outcome.reason}
        badge={
          confidence
            ? {
                text: confidence.words,
                warn: confidence.level !== "very_likely_better",
              }
            : undefined
        }
        aside={
          <>
            <h3>{won ? "What changed" : "What to do"}</h3>
            {won ? (
              <p>
                {candidate.label} was copied to <b>{outcome.copied_to}</b>. It
                is your best rule now.
              </p>
            ) : (
              <p>
                Nothing was copied. {previous.label} stays your best rule.
                {outcome.level === "ahead_not_proven"
                  ? " Run more test runs, then try again."
                  : ""}
              </p>
            )}
            <p style={{ marginTop: 10 }}>
              <a
                style={{ color: "var(--tf-hero-accent)" }}
                href={model.race_ref}
              >
                Open the full race
              </a>
            </p>
          </>
        }
      >
        {confidence ? (
          <p className="vk-lede" style={{ marginTop: 14 }}>
            {confidence.detail}
          </p>
        ) : null}
        <RunDots model={model} />
      </Hero>
      <Section
        id="proof"
        eyebrow="Proof"
        title="Can you trust this result?"
        intro={checkSummary(model.checks)}
      >
        <div className="vk-grid2">
          <Card title="Proof checklist" sub="Each line is a pass or a fail.">
            <ul className="vk-list" aria-label="Proof checklist">
              {model.checks.map((c) => (
                <li key={c.key}>
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      gap: 12,
                    }}
                  >
                    <span>{c.label}</span>
                    <Pill tone={c.pass ? "good" : "bad"}>
                      {c.pass ? "Pass" : "Fail"}
                    </Pill>
                  </div>
                  <p className="vk-note">{c.detail}</p>
                </li>
              ))}
            </ul>
          </Card>
          <Card
            title="How the two rules did"
            sub="Dollars per test run, with the likely range."
            takeaway={`The gain was ${signedMoney(model.versus.dollars?.mean)} per run. The likely range is ${rangeWords(model.versus.dollars?.low ?? null, model.versus.dollars?.high ?? null)}.`}
          >
            <table className="vk-table">
              <thead>
                <tr>
                  <th>Rule</th>
                  <th className="r">Rank</th>
                  <th className="r">Dollars per run</th>
                </tr>
              </thead>
              <tbody>
                {[candidate, previous].map((r, i) => (
                  <tr key={r.policy}>
                    <td>
                      {r.label}{" "}
                      <span className="vk-note">
                        {i === 0 ? "new" : "old best"}
                      </span>
                    </td>
                    <td className="r vk-num">{r.rank}</td>
                    <td className="r vk-num">{money(r.dollars.mean)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </div>
      </Section>
      <Section
        id="floor"
        eyebrow="On the floor"
        title={`What changes on the floor with ${candidate.label}?`}
        intro={`Compared with ${previous.label}, the old best.`}
      >
        <div className="vk-card">
          <ul className="vk-list" aria-label="Floor changes">
            {model.floor.map((f) => (
              <li key={f.key}>
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                  }}
                >
                  <span>{f.label}</span>
                  <span className="vk-num">
                    {f.unit === "share"
                      ? percent(f.baseline)
                      : plain(f.baseline)}{" "}
                    to{" "}
                    {f.unit === "share"
                      ? percent(f.recommended)
                      : plain(f.recommended)}
                  </span>
                </div>
                <p className="vk-note">{f.words}</p>
              </li>
            ))}
            {model.floor.length === 0 ? (
              <li>No floor facts were kept for these rules.</li>
            ) : null}
          </ul>
        </div>
      </Section>
      <Section id="details" eyebrow="Details" title="How this was made">
        <div className="vk-grid2">
          <Card title="Run facts">
            <dl className="vk-kv">
              <dt>Test runs</dt>
              <dd className="vk-num">{about.episodes}</dd>
              <dt>Seed</dt>
              <dd className="vk-num">{about.seed}</dd>
              <dt>Likely range</dt>
              <dd className="vk-num">{percent(about.interval)}</dd>
              <dt>New rule file</dt>
              <dd>{candidate.file}</dd>
              <dt>File fingerprint</dt>
              <dd className="vk-num" style={{ wordBreak: "break-all" }}>
                {candidate.sha256}
              </dd>
            </dl>
          </Card>
          <Card title="Problems">
            {model.problems.length ? (
              <Problems problems={model.problems} />
            ) : (
              <p>No problems.</p>
            )}
          </Card>
        </div>
      </Section>
    </Page>
  );
}
