// The states view (DESIGN.md 5, DATA-CONTRACT 3): which situations does the plant live in,
// and which ones cost money?
import { useMemo } from "react";
import { usd } from "../format";
import { Page, type SectionRef } from "../Page";
import { Card, EmptyState, Section } from "../primitives";
import { StateMapCard } from "../StateMapCard";
import type { StatesViewModel } from "../types";
import { useViewState } from "../viewState";
import { AnswerHero, Problems, Stepper, count, share } from "../answer";
import { moneyRows, nodeTitle, visibleEdges, visibleNodes } from "./derive";

export type StatesViewProps = {
  model: StatesViewModel;
  /** The workspace passes this to load another view. Offline files cannot. */
  onViewChange?: (view: string) => void;
};

function leakName(model: StatesViewModel, i: number): string {
  const leak = model.leaks[i];
  const node = model.chain.nodes.find((n) => n.id === leak.state_id);
  const words = leak.words ?? node?.words;
  const where = words?.setup ? `, ${words.setup.toLowerCase()}` : "";
  return `${words?.title ?? (node ? nodeTitle(node) : leak.state)}${where}`;
}

export function StatesView({ model, onViewChange }: StatesViewProps) {
  const { about, chain } = model;
  const built = chain.nodes.length;
  const [topText, setTop] = useViewState(
    "top",
    String(Math.min(about.top, built) || about.top),
  );
  const [probText, setProb] = useViewState("hide", String(about.min_prob));
  const top = Math.min(built || 1, Math.max(1, Number(topText) || about.top));
  const minProb = Math.min(1, Math.max(about.min_prob, Number(probText) || 0));
  const nodes = useMemo(() => visibleNodes(chain, top), [chain, top]);
  const ids = useMemo(() => new Set(nodes.map((n) => n.id)), [nodes]);
  const edges = useMemo(
    () => visibleEdges(chain, ids, minProb),
    [chain, ids, minProb],
  );
  const choices = useMemo(
    () => [
      {
        id: "chain",
        label: "Map",
        avgPerDay: model.avg_per_period,
        chain: {
          nodes,
          edges,
          coverage: nodes.reduce((sum, n) => sum + n.share, 0),
        },
      },
    ],
    [nodes, edges, model.avg_per_period],
  );
  const [selected, setSelected] = useViewState("state", "");
  const note = about.note?.replace(/\s*Add `state_groups`[^.]*\./, "").trim();
  const empty = built === 0;
  const shown = nodes.reduce((sum, n) => sum + n.share, 0);
  const rows = moneyRows(model.money);
  const sections: SectionRef[] = [
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
  const lead = model.leaks[0];
  return (
    <Page
      view="states"
      title="States"
      project={about.project}
      scope={
        empty ? "no moves" : `${about.moves} moves, ${about.view_name} view`
      }
      sections={sections}
      footer={
        <>
          twinflow states · {about.project} · {about.moves} moves
        </>
      }
    >
      <AnswerHero
        eyebrow={`What situations does the plant live in? · ${about.view_name} view`}
        headline={model.headline}
        lede={
          empty
            ? model.problems[0]?.fix
            : `The map shows the ${count(nodes.length, "most common situation")}. They cover ${share(shown)} of all days.`
        }
        aside={
          !empty && lead ? (
            <>
              <h3>Where it loses the most</h3>
              <p>
                {leakName(model, 0)}: {usd(lead.per_run)} per run, mostly{" "}
                {lead.cause_words}.
              </p>
              <p>{lead.try}</p>
            </>
          ) : undefined
        }
      />
      <div className="tf-wrap">
        {empty ? (
          <Section id="map" eyebrow="Map" question="No moves to draw">
            <EmptyState
              title="No moves were captured for this run."
              body={<p>{model.problems[0]?.fix}</p>}
            />
          </Section>
        ) : (
          <>
            <Section
              id="map"
              eyebrow="Map"
              question="What situations does the plant live in, and which ones cost money?"
            >
              <div className="tf-controls">
                <label className="tf-field">
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
                  max={built}
                  onChange={(n) => setTop(String(n))}
                />
                <Stepper
                  label="Hide rare moves under"
                  value={minProb}
                  min={about.min_prob}
                  max={1}
                  step={0.05}
                  format={share}
                  onChange={(n) => setProb(String(n))}
                />
              </div>
              {!onViewChange && (
                <p className="tf-note" style={{ marginBottom: 12 }}>
                  To see another view, run: twinflow states {about.project}{" "}
                  --view NAME --html
                </p>
              )}
              <StateMapCard
                chains={choices}
                horizon={Math.max(
                  1,
                  Math.round(about.moves / Math.max(1, about.episodes)),
                )}
                runs={about.episodes}
                levels={model.levels}
                families={model.families ?? []}
                lanesBy={model.levels === null ? "grid" : "auto"}
                selected={selected || null}
                onSelect={setSelected}
              />
              {note && (
                <p className="tf-note" style={{ marginTop: 10 }}>
                  {note}
                </p>
              )}
            </Section>
            <Section
              id="money"
              eyebrow="Money"
              question="Where does the money go, and which states are common?"
            >
              <div className="tf-grid2">
                <Card
                  question="Where does the money go?"
                  sub="Dollars per run, by part."
                  takeaway={`A normal day makes ${usd(model.avg_per_period)}.`}
                >
                  <ul className="tf-list" aria-label="Money by part">
                    {rows.map((r) => (
                      <li key={r.part}>
                        <div className="tf-row">
                          <span>{r.label}</span>
                          <span className="tf-num">{usd(r.value)}</span>
                        </div>
                        <div className="tf-track" aria-hidden="true">
                          <div
                            className={
                              "tf-bar " +
                              (r.part === "revenue" ? "tf-gain" : "tf-loss")
                            }
                            style={{
                              left: 0,
                              width: `${Math.round(r.width * 100)}%`,
                            }}
                          />
                        </div>
                      </li>
                    ))}
                  </ul>
                </Card>
                <Card question="Top states" sub="The most common situations.">
                  <div
                    className="tf-scroll"
                    tabIndex={0}
                    role="region"
                    aria-label="Top states table"
                  >
                    <table className="tf-table">
                      <thead>
                        <tr>
                          <th scope="col">State</th>
                          <th scope="col">Days</th>
                          <th scope="col">Money a day</th>
                          <th scope="col">Stays</th>
                        </tr>
                      </thead>
                      <tbody>
                        {nodes.map((n) => (
                          <tr key={n.id}>
                            <td>
                              {nodeTitle(n)}
                              {n.words?.setup && (
                                <div className="tf-note">{n.words.setup}</div>
                              )}
                            </td>
                            <td className="tf-num">{share(n.share)}</td>
                            <td className="tf-num">{usd(n.per_day)}</td>
                            <td className="tf-num">{share(n.stay)}</td>
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
        {model.leaks.length > 0 && (
          <Section
            id="leaks"
            eyebrow="Leaks"
            question="Where does the plant lose money?"
          >
            <Card>
              <ol className="tf-list" style={{ paddingLeft: 0 }}>
                {model.leaks.map((l, i) => (
                  <li key={l.state}>
                    <b>{leakName(model, i)}</b>{" "}
                    <span className="tf-pill tf-l">
                      {usd(l.per_run)} per run · {l.cause_words}
                    </span>
                    {l.try && <p className="tf-note">{l.try}</p>}
                  </li>
                ))}
              </ol>
            </Card>
          </Section>
        )}
        <Section id="details" eyebrow="Details" question="How this was made">
          <div className="tf-grid2">
            <Card question="Run facts">
              <dl className="tf-kv">
                <dt>Run folders</dt>
                <dd className="tf-num">{about.runs}</dd>
                <dt>Test runs</dt>
                <dd className="tf-num">{about.episodes}</dd>
                <dt>Moves</dt>
                <dd className="tf-num">{about.moves}</dd>
                <dt>States seen</dt>
                <dd className="tf-num">{about.states_seen}</dd>
                {about.time_span && (
                  <>
                    <dt>Time span</dt>
                    <dd className="tf-num">
                      {about.time_span.from} to {about.time_span.to}{" "}
                      {about.time_span.unit}
                    </dd>
                  </>
                )}
                <dt>View</dt>
                <dd>{about.view_name}</dd>
              </dl>
            </Card>
            <Card question="Problems and warnings">
              {model.problems.length ? (
                <Problems problems={model.problems} />
              ) : (
                <p>No problems.</p>
              )}
              {model.warnings.map((w) => (
                <p className="tf-note" key={w.text}>
                  {w.text} {w.fix}
                </p>
              ))}
            </Card>
          </div>
        </Section>
      </div>
    </Page>
  );
}
