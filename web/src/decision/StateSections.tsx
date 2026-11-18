// Race view sections: State map (DESIGN.md 3.8) and Money leaks (3.9).
// The chain and the selected state live in the URL (`map`, `state`).
import { LeakList } from "./LeakList";
import { Card, EmptyState, Section } from "./primitives";
import { raceChains, StateMapCard } from "./StateMapCard";
import { revealState } from "./StateMapChart";
import type { RaceViewModel } from "./types";
import { useViewState } from "./viewState";

export const STATE_SECTIONS = [
  { id: "states", label: "State map" },
  { id: "leaks", label: "Money leaks" },
];

export function StateSections({ model }: { model: RaceViewModel }) {
  const chains = raceChains(model);
  const pick = chains[0]?.id ?? "";
  const [chainId, setChainId] = useViewState("map", pick);
  const [selected, setSelected] = useViewState("state", "");
  const leaks = (pick && model.leaks?.[pick]) || [];

  const showState = (id: string) => {
    setChainId(pick);
    setSelected(id);
    requestAnimationFrame(() =>
      revealState(document.getElementById("states"), id),
    );
  };

  return (
    <>
      <Section
        id="states"
        eyebrow="State map"
        question="What situations does the plant live in, and which ones cost money?"
        hint="Each box is a situation: how full each family is, and what the press is set to. Click a box."
      >
        {chains.length ? (
          <StateMapCard
            chains={chains}
            horizon={model.about.horizon_days}
            runs={model.about.episodes}
            levels={model.views?.levels}
            families={model.families}
            chainId={chainId}
            onChainChange={(id) => {
              setChainId(id);
              setSelected("");
            }}
            selected={selected || null}
            onSelect={setSelected}
          />
        ) : (
          <EmptyState
            title="No moves were kept for this race."
            body={<p>Rerun the race. The pick's moves are always kept.</p>}
          />
        )}
      </Section>
      <Section
        id="leaks"
        eyebrow="Money leaks"
        question="Where does the pick still lose money?"
        hint="The five situations that cost the most per run, and what to try next. Click a row to see it on the map."
      >
        <Card>
          <LeakList
            leaks={leaks}
            levels={model.views?.levels}
            onSelectState={chains.length ? showState : undefined}
          />
        </Card>
      </Section>
    </>
  );
}
