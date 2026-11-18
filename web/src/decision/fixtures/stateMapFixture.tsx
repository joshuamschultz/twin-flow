// Fixtures page for the state map and money leaks (browser tests and screenshots).
// The test puts a race view model on window.__TF_FIXTURE__ before the page loads.
import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import "../../index.css";
import "../tokens.css";
import "../fonts/fonts.css";
import "../decision.css";
import { raceChains, StateMapCard } from "../StateMapCard";
import type { RaceViewModel, StateNode } from "../types";

const model = (window as unknown as { __TF_FIXTURE__?: RaceViewModel })
  .__TF_FIXTURE__;

const GRID_NODES: StateNode[] = [
  ["g0", 0.5, "m1:idle m2:idle"],
  ["g1", 0.3, "m1:busy m2:busy"],
  ["g2", 0.1, "m1:busy m2:idle"],
  ["g3", 0.05, "m1:down m2:idle"],
].map(([id, share, label]) => ({
  id: id as string,
  label: label as string,
  parts_of_label: null,
  share: share as number,
  visits: Math.round((share as number) * 400),
  stay: 0.5,
  per_day: 800 - (share as number) * 1000,
  money_per_day: { revenue: 2000, production: 900, holding: 100 },
}));
const GRID_EDGES = [
  { from: "g0", to: "g1", prob: 0.3, self: false },
  { from: "g1", to: "g0", prob: 0.4, self: false },
  { from: "g1", to: "g2", prob: 0.2, self: false },
  { from: "g2", to: "g3", prob: 0.1, self: false },
];

function RaceFixture({ m }: { m: RaceViewModel }) {
  const chains = raceChains(m);
  const [chainId, setChainId] = useState(chains[0]?.id);
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <section data-fixture="statemap" style={{ marginBottom: 40 }}>
      <StateMapCard
        chains={chains}
        horizon={m.about.horizon_days}
        runs={m.about.episodes}
        levels={m.views.levels}
        families={m.families}
        chainId={chainId}
        onChainChange={(id) => {
          setChainId(id);
          setSelected(null);
        }}
        selected={selected}
        onSelect={setSelected}
      />
    </section>
  );
}

function Fixtures() {
  if (!model) return <p>No fixture data.</p>;
  return (
    <main className="tf-root tf-wrap" style={{ padding: "24px 32px" }}>
      <RaceFixture m={model} />
      <section data-fixture="statemap-grid">
        <StateMapCard
          chains={[
            {
              id: "grid",
              label: "Run",
              chain: { nodes: GRID_NODES, edges: GRID_EDGES, coverage: 0.95 },
              avgPerDay: 600,
            },
          ]}
          horizon={60}
        />
      </section>
    </main>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Fixtures />
  </React.StrictMode>,
);
