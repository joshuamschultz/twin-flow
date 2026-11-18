// The states view (DESIGN.md 5, DATA-CONTRACT 3): which situations does the plant live in,
// and which ones cost money?
import { useMemo, useState } from "react";
import type { StatesViewModel } from "../types";
import {
  Card,
  Hero,
  Missing,
  Page,
  Pill,
  Problems,
  Section,
  Stepper,
} from "../viewkit";
import { count, money, percent } from "../viewkit/format";
import {
  lanesOf,
  moneyRows,
  nodeTitle,
  visibleEdges,
  visibleNodes,
} from "./derive";
import { StatesMapSlot } from "./StatesMapSlot";

export type StatesViewProps = {
  model: StatesViewModel;
  /** The workspace passes this to load another view. Offline files cannot. */
  onViewChange?: (view: string) => void;
};

function leakName(model: StatesViewModel, i: number): string {
  const leak = model.leaks[i];
  const node = model.chain.nodes.find((n) => n.id === leak.state_id);
  const where = node?.words?.setup ? `, ${node.words.setup.toLowerCase()}` : "";
  return `${node ? nodeTitle(node) : leak.state}${where}`;
}

export function StatesView({ model, onViewChange }: StatesViewProps) {
  const { about, chain } = model;
  const [top, setTop] = useState(
    Math.min(about.top, chain.nodes.length) || about.top,
  );
  const [minProb, setMinProb] = useState(about.min_prob);
  const nodes = useMemo(() => visibleNodes(chain, top), [chain, top]);
  const ids = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);
  const edges = useMemo(
    () => visibleEdges(chain, ids, minProb),
    [chain, ids, minProb],
  );
  const lanes = useMemo(
    () => lanesOf(nodes, model.levels !== null),
    [nodes, model.levels],
  );
  const empty = chain.nodes.length === 0;
  const shown = nodes.reduce((sum, n) => sum + n.share, 0);
  const rows = moneyRows(model.money);
  const sections = [
    { id: "answer", label: "Answer" },
    ...(empty
      ? []
      : [
          { id: "map", label: "Map" },
          { id: "money", label: "Money" },
        ]),
    ...(model.leaks.length ? [{ id: "leaks", label: "Leaks" }] : []),
    { id: "details", label: "Details" },
  ];
  return (
    <Page
      view="states"
      viewName="States"
      project={about.project}
      scope={
        empty ? "no moves" : `${about.moves} moves, ${about.view_name} view`
      }
      sections={sections}
    >
      <Hero
        eyebrow={`What situations does the plant live in? . ${about.view_name} view`}
        headline={model.headline}
        lede={
          empty
            ? (model.problems[0]?.fix ?? "")
            : `The map shows the ${count(nodes.length, "most common situation")}. They cover ${percent(shown)} of all days.`
        }
        aside={
          !empty && model.leaks[0] ? (
            <>
              <h3>Where it loses the most</h3>
              <p>
                {leakName(model, 0)}: {money(model.leaks[0].per_run)} per run,
                mostly {model.leaks[0].cause_words}.
              </p>
              <p style={{ marginTop: 8 }}>{model.leaks[0].try}</p>
            </>
          ) : undefined
        }
        badge={
          about.note
            ? { text: "Read the note under the map", warn: true }
            : undefined
        }
      />
      {empty ? (
        <Section id="map" eyebrow="Map" title="No moves to draw">
          <Missing
            said="No moves were captured for this run."
            fix={model.problems[0]?.fix}
          />
        </Section>
      ) : (
        <>
          <Section
            id="map"
            eyebrow="Map"
            title="What situations does the plant live in, and which ones cost money?"
          >
            <div className="vk-controls">
              <label className="vk-field">
                <span>View</span>
                <select
                  value={about.view_name}
                  disabled={!onViewChange}
                  onChange={(e) => onViewChange?.(e.target.value)}
                  aria-label="View"
                >
                  {about.views_available.map((v) => (
                    <option key={v} value={v}>
                      {v}
                    </option>
                  ))}
                </select>
              </label>
              <Stepper
                label="Top states"
                value={top}
                min={1}
                max={chain.nodes.length}
                onChange={setTop}
              />
              <Stepper
                label="Hide rare moves under"
                value={minProb}
                min={0}
                max={1}
                step={0.05}
                format={percent}
                onChange={setMinProb}
              />
            </div>
            {!onViewChange ? (
              <p className="vk-note" style={{ marginBottom: 12 }}>
                To see another view, run: twinflow states {about.project} --view
                NAME --html
              </p>
            ) : null}
            <div className="vk-card">
              <StatesMapSlot
                nodes={nodes}
                edges={edges}
                lanes={lanes}
                average={model.avg_per_period}
              />
              <p className="vk-takeaway">
                The map shows the {count(nodes.length, "most common state")}.
                They cover {percent(shown)} of all days.
                {about.note ? ` ${about.note}` : ""}
              </p>
            </div>
          </Section>
          <Section
            id="money"
            eyebrow="Money"
            title="Where does the money go, and which states are common?"
          >
            <div className="vk-grid2">
              <Card
                title="Where does the money go?"
                sub="Dollars per run, by part."
                takeaway={`A normal day makes ${money(model.avg_per_period)}.`}
              >
                <ul className="vk-list" aria-label="Money by part">
                  {rows.map((r) => (
                    <li key={r.part}>
                      <div
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                        }}
                      >
                        <span>{r.label}</span>
                        <span className="vk-num">{money(r.value)}</span>
                      </div>
                      <div
                        className={`vk-bar${r.part === "revenue" ? "" : " loss"}`}
                      >
                        <i style={{ width: `${Math.round(r.width * 100)}%` }} />
                      </div>
                    </li>
                  ))}
                </ul>
              </Card>
              <Card title="Top states" sub="The most common situations.">
                <div className="vk-scroll">
                  <table className="vk-table">
                    <thead>
                      <tr>
                        <th>State</th>
                        <th className="r">Days</th>
                        <th className="r">Money a day</th>
                        <th className="r">Stays</th>
                      </tr>
                    </thead>
                    <tbody>
                      {nodes.map((n) => (
                        <tr key={n.id}>
                          <td>
                            {nodeTitle(n)}
                            {n.words?.setup ? (
                              <div className="vk-note">{n.words.setup}</div>
                            ) : null}
                          </td>
                          <td className="r vk-num">{percent(n.share)}</td>
                          <td className="r vk-num">{money(n.per_day)}</td>
                          <td className="r vk-num">{percent(n.stay)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </Card>
            </div>
          </Section>
        </>
      )}
      {model.leaks.length ? (
        <Section
          id="leaks"
          eyebrow="Leaks"
          title="Where does the plant lose money?"
        >
          <div className="vk-card">
            <ol className="vk-list" style={{ paddingLeft: 0 }}>
              {model.leaks.map((l) => (
                <li key={l.state}>
                  <b>
                    {chain.nodes.find((n) => n.id === l.state_id)?.words
                      ?.title ?? l.state}
                  </b>{" "}
                  <Pill tone="bad">
                    {money(l.per_run)} per run . {l.cause_words}
                  </Pill>
                  {l.try ? <p className="vk-note">{l.try}</p> : null}
                </li>
              ))}
            </ol>
          </div>
        </Section>
      ) : null}
      <Section id="details" eyebrow="Details" title="How this was made">
        <div className="vk-grid2">
          <Card title="Run facts">
            <dl className="vk-kv">
              <dt>Run folders</dt>
              <dd className="vk-num">{about.runs}</dd>
              <dt>Test runs</dt>
              <dd className="vk-num">{about.episodes}</dd>
              <dt>Moves</dt>
              <dd className="vk-num">{about.moves}</dd>
              <dt>States seen</dt>
              <dd className="vk-num">{about.states_seen}</dd>
              {about.time_span ? (
                <>
                  <dt>Time span</dt>
                  <dd className="vk-num">
                    {about.time_span.from} to {about.time_span.to}{" "}
                    {about.time_span.unit}
                  </dd>
                </>
              ) : null}
              <dt>View</dt>
              <dd>{about.view_name}</dd>
            </dl>
          </Card>
          <Card title="Problems and warnings">
            {model.problems.length ? (
              <Problems problems={model.problems} />
            ) : (
              <p>No problems.</p>
            )}
            {model.warnings.map((w) => (
              <p className="vk-note" key={w.text}>
                {w.text} {w.fix}
              </p>
            ))}
          </Card>
        </div>
      </Section>
      <div className="vk-wrap vk-foot">
        Made by twinflow {model.twinflow_version ?? ""}{" "}
        {model.generated_at ?? ""}
      </div>
    </Page>
  );
}
