// PLACEHOLDER for the state map. The real StateMap (lanes, glyphs, arrows, side panel) is
// built on branch feat/TF-006-ui-statemap and replaces this file at merge. The props are the
// chain data on purpose, so the swap is one import: nodes, edges and lanes.
import { useState } from "react";
import type { StatesStateEdge, StatesStateNode } from "../types";
import { money, percent } from "../viewkit/format";
import { nodeTitle, type Lanes } from "./derive";

export type StatesMapSlotProps = {
  nodes: StatesStateNode[];
  edges: StatesStateEdge[];
  lanes: Lanes;
  /** Money a normal day makes. A box is "better" or "worse" against it. */
  average?: number;
};

function box(node: StatesStateNode, average: number | undefined): string {
  if (node.losing) return "loses";
  if (average === undefined || node.per_day === undefined) return "same";
  return node.per_day >= average ? "better" : "worse";
}

export function StatesMapSlot({
  nodes,
  edges,
  lanes,
  average,
}: StatesMapSlotProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out = edges.filter((e) => e.from === picked && !e.self);
  const stay = nodes.find((n) => n.id === picked)?.stay;
  return (
    <div data-testid="states-map-slot">
      <div
        role="group"
        aria-label={`Situations in ${lanes.lanes.length} lanes, grouped by ${lanes.kind}`}
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${Math.min(lanes.lanes.length, 3)}, minmax(0, 1fr))`,
          gap: 12,
        }}
      >
        {lanes.lanes.map((lane) => (
          <div key={lane.name} data-lane={lane.name}>
            <p className="vk-eyebrow" style={{ marginBottom: 8 }}>
              {lane.name}
            </p>
            <div className="vk-list">
              {lane.nodes.map((n) => (
                <div
                  key={n.id}
                  role="button"
                  tabIndex={0}
                  aria-pressed={picked === n.id}
                  data-state={box(n, average)}
                  className="vk-box"
                  onClick={() => setPicked(n.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setPicked(n.id);
                    }
                  }}
                  style={{
                    border: `2px solid ${
                      box(n, average) === "loses"
                        ? "var(--tf-loss)"
                        : box(n, average) === "better"
                          ? "var(--tf-pick)"
                          : "var(--tf-today)"
                    }`,
                    borderRadius: 4,
                    padding: "8px 10px",
                    background:
                      picked === n.id
                        ? "var(--ba-tint-strong)"
                        : "var(--ba-surface)",
                    cursor: "pointer",
                  }}
                >
                  <b style={{ color: "var(--ba-headline)" }}>{nodeTitle(n)}</b>
                  <div className="vk-num" style={{ fontSize: 13 }}>
                    {percent(n.share)} . {money(n.per_day)} a day
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div aria-live="polite" style={{ marginTop: 14 }}>
        {picked && byId.get(picked) ? (
          <div className="vk-card" data-testid="states-next">
            <h3>
              Where the plant goes next from "{nodeTitle(byId.get(picked)!)}"
            </h3>
            <p className="vk-sub">Stays here {percent(stay)} of the time.</p>
            <ul className="vk-list">
              {out.map((e) => (
                <li key={`${e.from}-${e.to}`}>
                  {percent(e.prob)} to {nodeTitle(byId.get(e.to)!)}
                </li>
              ))}
              {out.length === 0 ? (
                <li>No likely moves to other states.</li>
              ) : null}
            </ul>
          </div>
        ) : (
          <p className="vk-note">
            Pick a box to see where the plant goes next.
          </p>
        )}
      </div>
      <details style={{ marginTop: 14 }}>
        <summary>Show moves as a table</summary>
        <div className="vk-scroll">
          <table className="vk-table">
            <thead>
              <tr>
                <th>From</th>
                <th>To</th>
                <th className="r">Chance</th>
              </tr>
            </thead>
            <tbody>
              {edges.map((e) => (
                <tr key={`${e.from}-${e.to}`}>
                  <td>{nodeTitle(byId.get(e.from)!)}</td>
                  <td>{e.self ? "Stays here" : nodeTitle(byId.get(e.to)!)}</td>
                  <td className="r vk-num">{percent(e.prob)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
