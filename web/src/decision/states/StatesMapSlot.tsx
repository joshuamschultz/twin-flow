// PLACEHOLDER for the state map. The real StateMap (lanes, glyphs, arrows, side panel) is
// built on branch feat/TF-006-ui-statemap and replaces this file at merge. The props are the
// chain data on purpose, so the swap is one import: nodes, edges and lanes.
import { useState } from "react";
import { usd } from "../format";
import type { StatesStateEdge, StatesStateNode } from "../types";
import { share } from "../viewkit/words";
import { nodeTitle, type Lanes } from "./derive";

export type StatesMapSlotProps = {
  nodes: StatesStateNode[];
  edges: StatesStateEdge[];
  lanes: Lanes;
  /** Money a normal day makes. A box is "better" or "worse" against it. */
  average?: number;
};

function boxState(node: StatesStateNode, average: number | undefined): string {
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
  const current = picked ? byId.get(picked) : undefined;
  const out = edges.filter((e) => e.from === picked && !e.self);
  return (
    <div data-testid="states-map-slot">
      <div
        className="vk-lane-grid"
        role="group"
        aria-label={`Situations in ${lanes.lanes.length} lanes, grouped by ${lanes.kind}`}
        style={{
          gridTemplateColumns: `repeat(${Math.min(lanes.lanes.length, 3)}, minmax(0, 1fr))`,
        }}
      >
        {lanes.lanes.map((lane) => (
          <div key={lane.name} data-lane={lane.name}>
            <div className="tf-eyebrow" style={{ marginBottom: 8 }}>
              {lane.name}
            </div>
            <div className="vk-list">
              {lane.nodes.map((n) => (
                <div
                  key={n.id}
                  role="button"
                  tabIndex={0}
                  aria-pressed={picked === n.id}
                  data-state={boxState(n, average)}
                  className="vk-box"
                  onClick={() => setPicked(n.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setPicked(n.id);
                    }
                  }}
                >
                  <b>{nodeTitle(n)}</b>
                  <div className="tf-num" style={{ fontSize: 13 }}>
                    {share(n.share)} . {usd(n.per_day)} a day
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
      <div aria-live="polite" style={{ marginTop: 14 }}>
        {current ? (
          <div className="tf-card" data-testid="states-next">
            <h3>Where the plant goes next from "{nodeTitle(current)}"</h3>
            <p className="tf-sub">
              Stays here {share(current.stay)} of the time.
            </p>
            <ul className="vk-list">
              {out.map((e) => (
                <li key={`${e.from}-${e.to}`}>
                  {share(e.prob)} to {nodeTitle(byId.get(e.to)!)}
                </li>
              ))}
              {out.length === 0 && <li>No likely moves to other states.</li>}
            </ul>
          </div>
        ) : (
          <p className="tf-note">
            Pick a box to see where the plant goes next.
          </p>
        )}
      </div>
      <details style={{ marginTop: 14 }}>
        <summary>Show moves as a table</summary>
        <div
          className="tf-scroll"
          tabIndex={0}
          role="region"
          aria-label="Moves table"
        >
          <table className="tf-table">
            <thead>
              <tr>
                <th scope="col">From</th>
                <th scope="col">To</th>
                <th scope="col" className="tf-r">
                  Chance
                </th>
              </tr>
            </thead>
            <tbody>
              {edges.map((e) => (
                <tr key={`${e.from}-${e.to}`}>
                  <td>{nodeTitle(byId.get(e.from)!)}</td>
                  <td>{e.self ? "Stays here" : nodeTitle(byId.get(e.to)!)}</td>
                  <td className="tf-r tf-num">{share(e.prob)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
