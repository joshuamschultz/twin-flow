// The project view (DESIGN.md 7, DATA-CONTRACT 5): is this twin ready to trust, and what do
// I fix first?
import type { ProjectViewModel } from "../types";
import { Card, Hero, Missing, Page, Pill, Problems, Section } from "../viewkit";
import { count } from "../viewkit/format";
import { ruleCounts, viewsByKind } from "./derive";

export function ProjectView({ model }: { model: ProjectViewModel }) {
  const { about, status, twin, rules, score } = model;
  const counts = ruleCounts(rules);
  const { stateViews, observations } = viewsByKind(model.views);
  const hasProblems = model.problems.length > 0;
  const ready = status.level === "ready";
  const sections = [
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
      viewName="Project"
      project={about.project}
      scope={about.capsule}
      sections={sections}
    >
      <Hero
        eyebrow={`Project . ${about.project}`}
        headline={status.headline}
        lede={about.summary}
        badge={
          ready
            ? { text: "Ready to race" }
            : {
                text: status.level === "broken" ? "Cannot open" : "Needs fixes",
                warn: true,
              }
        }
      />
      {hasProblems ? (
        <Section
          id="problems"
          eyebrow="Fix first"
          title="Problems to fix"
          intro="Errors come first. Each one says where it is and how to fix it."
        >
          <Problems problems={model.problems} />
        </Section>
      ) : null}
      <Section id="twin" eyebrow="The twin" title="What does the twin hold?">
        <div className="vk-grid2">
          <Card title="Shape">
            <dl className="vk-kv">
              <dt>Lines</dt>
              <dd>
                {twin.lines.length
                  ? twin.lines.map((l) => l.label).join(", ")
                  : "none"}
              </dd>
              <dt>Products</dt>
              <dd className="vk-num">{twin.products}</dd>
              <dt>Families</dt>
              <dd>
                {twin.families.length
                  ? twin.families
                      .map((f) => `${f.name} (${f.products})`)
                      .join(", ")
                  : "none"}
              </dd>
              <dt>One run</dt>
              <dd className="vk-num">
                {twin.horizon.value} {twin.horizon.unit}
              </dd>
            </dl>
          </Card>
          <Card title="What things cost">
            {twin.costs.length ? (
              <dl className="vk-kv">
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
        title="Which rules can race, and how are they scored?"
      >
        <div className="vk-list">
          <Card
            title="Rules"
            sub={`${count(counts.own, "rule")} of your own, ${counts.builtIn} built in.${
              counts.today ? ` Today you run: ${counts.today}.` : ""
            }`}
          >
            {model.rules_note ? <Missing said={model.rules_note} /> : null}
            <div className="vk-scroll">
              <table className="vk-table">
                <thead>
                  <tr>
                    <th>Rule</th>
                    <th>Where</th>
                    <th className="r">Settings tried</th>
                  </tr>
                </thead>
                <tbody>
                  {rules.map((r) => (
                    <tr key={r.policy}>
                      <td>
                        {r.label}{" "}
                        {r.is_baseline ? (
                          <Pill tone="neutral">Today</Pill>
                        ) : null}{" "}
                        {r.ok ? null : <Pill tone="bad">Problem</Pill>}
                        <div className="vk-note">{r.about}</div>
                      </td>
                      <td>{r.file ?? "built in"}</td>
                      <td className="r vk-num">{r.settings_tried}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
          <Card title="Score and views">
            <dl className="vk-kv">
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
            <ul className="vk-list" style={{ marginTop: 12 }}>
              {stateViews.map((v) => (
                <li key={v.name} className="vk-note">
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
        title="What did the last runs say?"
      >
        {model.recent.length ? (
          <div className="vk-card">
            <ul className="vk-list">
              {model.recent.map((r) => (
                <li
                  key={r.id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 12,
                  }}
                >
                  <span>
                    <span className="vk-note">{r.when.slice(0, 10)}</span>{" "}
                    {r.sentence}
                  </span>
                  {r.href ? <a href={r.href}>Open</a> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <Missing
            said="No results yet."
            fix="Run a race with `twinflow policies race`, then make this page again."
          />
        )}
      </Section>
      <Section id="details" eyebrow="Details" title="Files and notes">
        <div className="vk-grid2">
          <Card title="Your own files">
            {model.extensions.length ? (
              <ul className="vk-list">
                {model.extensions.map((e) => (
                  <li key={`${e.kind}-${e.name}`}>
                    {e.kind} <b>{e.name}</b>{" "}
                    <span className="vk-note">{e.file}</span>{" "}
                    {e.ok ? null : <Pill tone="bad">Problem</Pill>}
                  </li>
                ))}
              </ul>
            ) : (
              <p>No engines, scores or features of your own.</p>
            )}
          </Card>
          <Card title="Assumptions">
            {twin.assumptions.length ? (
              <ul className="vk-list">
                {twin.assumptions.map((a) => (
                  <li key={a}>{a}</li>
                ))}
              </ul>
            ) : (
              <p>No assumptions were written down.</p>
            )}
            {twin.provenance ? (
              <p className="vk-note">Source: {twin.provenance}</p>
            ) : null}
            <p className="vk-note">Folder: {about.folder}</p>
          </Card>
        </div>
      </Section>
    </Page>
  );
}
