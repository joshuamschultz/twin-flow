// The project view (DESIGN.md 7, DATA-CONTRACT 5): is this twin ready to trust, and what do
// I fix first?
import { Page, type SectionRef } from "../Page";
import { Card, EmptyState, Section, Tag } from "../primitives";
import type { ProjectViewModel } from "../types";
import { Problems, count } from "../answer";
import { AnswerHero } from "../AnswerHero";
import { ruleCounts, viewsByKind } from "./derive";

export function ProjectView({ model }: { model: ProjectViewModel }) {
  const { about, status, twin, rules, score } = model;
  const counts = ruleCounts(rules);
  const { stateViews, observations } = viewsByKind(model.views);
  const hasProblems = model.problems.length > 0;
  const ready = status.level === "ready";
  const sections: SectionRef[] = [
    { id: "answer", label: "Answer" },
    ...(hasProblems ? [{ id: "problems", label: "Problems" }] : []),
    { id: "twin", label: "The twin" },
    { id: "rules", label: "Rules" },
    { id: "results", label: "Last results" },
    { id: "details", label: "Details" },
  ];
  return (
    <Page
      view="project"
      title="Project"
      project={about.project}
      scope={about.capsule}
      sections={sections}
      footer={<>twinflow project · {about.project}</>}
    >
      <AnswerHero
        id="answer"
        eyebrow={`Project · ${about.project}`}
        headline={status.headline}
        lede={about.summary}
        badge={{
          level: ready ? "very_likely_better" : "maybe_worse",
          words: ready
            ? "Ready to race"
            : status.level === "broken"
              ? "Cannot open"
              : "Needs fixes",
        }}
      />
      <div className="tf-wrap">
        {hasProblems && (
          <Section
            id="problems"
            eyebrow="Fix first"
            question="Problems to fix"
            hint="Errors come first. Each one says where it is and how to fix it."
          >
            <Problems problems={model.problems} />
          </Section>
        )}
        <Section
          id="twin"
          eyebrow="The twin"
          question="What does the twin hold?"
        >
          <div className="tf-grid2">
            <Card question="Shape">
              <dl className="tf-kv">
                <dt>Lines</dt>
                <dd>
                  {twin.lines.length
                    ? twin.lines.map((l) => l.label).join(", ")
                    : "none"}
                </dd>
                <dt>Products</dt>
                <dd className="tf-num">{twin.products}</dd>
                <dt>Families</dt>
                <dd>
                  {twin.families.length
                    ? twin.families
                        .map((f) => `${f.name} (${f.products})`)
                        .join(", ")
                    : "none"}
                </dd>
                <dt>One run</dt>
                <dd className="tf-num">
                  {twin.horizon.value} {twin.horizon.unit}
                </dd>
              </dl>
            </Card>
            <Card question="What things cost">
              {twin.costs.length ? (
                <dl className="tf-kv">
                  {twin.costs.map((c) => (
                    <div key={c.label} style={{ display: "contents" }}>
                      <dt>{c.label}</dt>
                      <dd>{c.value}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p>No costs were found.</p>
              )}
            </Card>
          </div>
        </Section>
        <Section
          id="rules"
          eyebrow="Rules"
          question="Which rules can race, and how are they scored?"
        >
          <div className="tf-list">
            <Card
              question="Rules"
              sub={`${count(counts.own, "rule")} of your own, ${counts.builtIn} built in.${
                counts.today ? ` Today you run: ${counts.today}.` : ""
              }`}
            >
              {model.rules_note && <EmptyState title={model.rules_note} />}
              <div
                className="tf-scroll"
                tabIndex={0}
                role="region"
                aria-label="Rules table"
              >
                <table className="tf-table">
                  <thead>
                    <tr>
                      <th scope="col">Rule</th>
                      <th scope="col">Where</th>
                      <th scope="col">Settings tried</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rules.map((r) => (
                      <tr key={r.policy}>
                        <td>
                          {r.label}{" "}
                          {r.is_baseline && <Tag kind="today">Today</Tag>}{" "}
                          {!r.ok && <Tag kind="plain">Problem</Tag>}
                          <div className="tf-note">{r.about}</div>
                        </td>
                        <td>{r.file ?? "built in"}</td>
                        <td className="tf-num">{r.settings_tried}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </Card>
            <Card question="Score and views">
              <dl className="tf-kv">
                <dt>Score</dt>
                <dd>
                  {score.is_dollars ? "dollars" : score.name}
                  {score.file ? ` (${score.file})` : ""}
                </dd>
                <dt>State views</dt>
                <dd>{stateViews.map((v) => v.name).join(", ") || "none"}</dd>
                <dt>What rules see</dt>
                <dd>
                  {observations.map((v) => v.name).join(", ") || "everything"}
                </dd>
              </dl>
              <ul className="tf-list" style={{ marginTop: 12 }}>
                {stateViews.map((v) => (
                  <li key={v.name} className="tf-note">
                    <b>{v.name}</b>: {v.about}
                  </li>
                ))}
              </ul>
            </Card>
          </div>
        </Section>
        <Section
          id="results"
          eyebrow="Last results"
          question="What did the last runs say?"
        >
          {model.recent.length ? (
            <Card>
              <ul className="tf-list">
                {model.recent.map((r) => (
                  <li key={r.id} className="tf-row">
                    <span>
                      <span className="tf-note">{r.when.slice(0, 10)}</span>{" "}
                      {r.sentence}
                    </span>
                    {r.href && <a href={r.href}>Open</a>}
                  </li>
                ))}
              </ul>
            </Card>
          ) : (
            <EmptyState
              title="No results yet."
              fix="twinflow policies race <project>"
              body={<p>Run a race, then make this page again.</p>}
            />
          )}
        </Section>
        <Section id="details" eyebrow="Details" question="Files and notes">
          <div className="tf-grid2">
            <Card question="Your own files">
              {model.extensions.length ? (
                <ul className="tf-list">
                  {model.extensions.map((e) => (
                    <li key={`${e.kind}-${e.name}`}>
                      {e.kind} <b>{e.name}</b>{" "}
                      <span className="tf-note">{e.file}</span>{" "}
                      {!e.ok && <Tag kind="plain">Problem</Tag>}
                    </li>
                  ))}
                </ul>
              ) : (
                <p>No engines, scores or features of your own.</p>
              )}
            </Card>
            <Card question="Assumptions">
              {twin.assumptions.length ? (
                <ul className="tf-list">
                  {twin.assumptions.map((a) => (
                    <li key={a}>{a}</li>
                  ))}
                </ul>
              ) : (
                <p>No assumptions were written down.</p>
              )}
              {twin.provenance && (
                <p className="tf-note">Source: {twin.provenance}</p>
              )}
              <p className="tf-note">Folder: {about.folder}</p>
            </Card>
          </div>
        </Section>
      </div>
    </Page>
  );
}
