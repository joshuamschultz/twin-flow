// The side panel for the selected state (DESIGN.md 3.8).
import { forwardRef } from "react";
import { MINUS, pct, usd } from "./format";
import { nodeWords, wordsSentence, type LevelDef } from "./stateMap";
import type { StateEdge, StateNode } from "./types";

const PART_WORDS: [key: string, words: string, gain: boolean][] = [
  ["revenue", "Sales", true],
  ["production", "Making product", false],
  ["setup", "Changeovers", false],
  ["holding", "Keeping stock", false],
  ["lost_sales", "Lost sales", false],
  ["late", "Late orders", false],
];

function partColor(key: string, gain: boolean): string {
  if (gain) return "var(--tf-gain-fill)";
  return key === "setup" || key === "lost_sales"
    ? "var(--tf-loss)"
    : "var(--tf-today)";
}

export type StatePanelProps = {
  node: StateNode;
  chain: { nodes: StateNode[]; edges: StateEdge[] };
  avgPerDay: number;
  /** Days in one run. */
  horizon: number;
  /** Test runs behind the numbers, for the "seen" line. */
  runs?: number;
  levels?: readonly (LevelDef | string)[] | null;
  onSelect: (id: string) => void;
};

export const StatePanel = forwardRef<HTMLHeadingElement, StatePanelProps>(
  function StatePanel(
    { node, chain, avgPerDay, horizon, runs, levels, onSelect },
    ref,
  ) {
    const onMap = new Map(chain.nodes.map((n) => [n.id, n]));
    const out = chain.edges.filter((e) => e.from === node.id);
    const selfEdge = out.find((e) => e.self || e.to === node.id);
    const stay = selfEdge?.prob ?? node.stay ?? 0;
    const nexts = out
      .filter((e) => e.to !== node.id && !e.self && onMap.has(e.to))
      .sort((a, b) => b.prob - a.prob);
    const rare = Math.max(0, 1 - stay - nexts.reduce((a, e) => a + e.prob, 0));
    const money = node.money_per_day ?? {};
    const parts = PART_WORDS.filter(([k]) => (money[k] ?? 0) > 0.5);
    const max = Math.max(1, ...parts.map(([k]) => money[k]));
    const days = Math.round(node.share * horizon);

    return (
      <div className="tf-sm-panel" data-state-panel="" aria-live="polite">
        <div className="tf-sm-eyebrow">Selected state</div>
        <h3 ref={ref} tabIndex={-1}>
          {wordsSentence(nodeWords(node, levels))}
        </h3>
        <div className="tf-sm-kv">
          <div>
            <div className="tf-sm-k">Time spent here</div>
            <div className="tf-sm-big" data-testid="time">
              {pct(node.share)}
            </div>
            <div className="tf-sm-note">
              {days < 1
                ? "Less than a day in a run"
                : `About ${days} ${days === 1 ? "day" : "days"} in a run`}
            </div>
          </div>
          <div>
            <div className="tf-sm-k">Money made per day here</div>
            <div
              className={`tf-sm-big${node.per_day < 0 ? " loss" : ""}`}
              data-testid="money"
            >
              {usd(node.per_day)}
            </div>
            <div className="tf-sm-note">
              A normal day makes {usd(avgPerDay)}
            </div>
          </div>
        </div>

        <h4>Where the plant goes next</h4>
        <ul className="tf-sm-next">
          <li className="tf-sm-nx" data-testid="stay">
            <span>Stays here</span>
            <span className="tf-sm-bar" aria-hidden="true">
              <i
                style={{
                  width: `${stay * 100}%`,
                  background: "var(--tf-today)",
                }}
              />
            </span>
            <span className="tf-sm-num">{pct(stay, 0)}</span>
          </li>
          {nexts.map((e) => {
            const words = wordsSentence(nodeWords(onMap.get(e.to)!, levels));
            return (
              <li className="tf-sm-nx" key={e.to}>
                <button
                  type="button"
                  className="tf-sm-link"
                  data-next-state={e.to}
                  title={words}
                  onClick={() => onSelect(e.to)}
                >
                  {words}
                </button>
                <span className="tf-sm-bar" aria-hidden="true">
                  <i style={{ width: `${e.prob * 100}%` }} />
                </span>
                <span className="tf-sm-num">{pct(e.prob, 0)}</span>
              </li>
            );
          })}
          {rare >= 0.005 && (
            <li className="tf-sm-nx" data-testid="rare">
              <span className="tf-sm-muted">Rare states, not on the map</span>
              <span className="tf-sm-bar" aria-hidden="true">
                <i
                  style={{
                    width: `${rare * 100}%`,
                    background: "var(--tf-axis)",
                  }}
                />
              </span>
              <span className="tf-sm-num">{pct(rare, 0)}</span>
            </li>
          )}
        </ul>

        {parts.length > 0 && (
          <>
            <h4>Money on a day here</h4>
            <ul className="tf-sm-parts">
              {parts.map(([k, words, gain]) => (
                <li className="tf-sm-mrow" key={k}>
                  <span>{words}</span>
                  <span className="tf-sm-bar" aria-hidden="true">
                    <i
                      style={{
                        width: `${(money[k] / max) * 100}%`,
                        background: partColor(k, gain),
                      }}
                    />
                  </span>
                  <span className="tf-sm-num">
                    {gain ? "" : MINUS}
                    {usd(money[k])}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
        <p className="tf-sm-note">
          Seen {node.visits.toLocaleString("en-US")}{" "}
          {node.visits === 1 ? "time" : "times"}
          {runs ? ` in ${runs} runs` : ""}.
        </p>
      </div>
    );
  },
);
