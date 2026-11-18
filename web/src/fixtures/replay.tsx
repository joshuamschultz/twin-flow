// The replay strips and lanes, alone (B-18, B-19).
import { replayRuns } from "../decision/replay";
import { ReplayLanes } from "../decision/ReplayLanes";
import { StripStack } from "../decision/ReplayStrips";
import type { RaceViewModel } from "../decision/types";

export function LanesFixture({ model }: { model: RaceViewModel }) {
  return (
    <ReplayLanes
      runs={replayRuns(model)}
      products={model.products}
      families={model.families}
      economics={model.economics}
    />
  );
}

export function StripsFixture({ model }: { model: RaceViewModel }) {
  return (
    <>
      {replayRuns(model).map((r) => (
        <section key={r.id} data-lane={r.id} style={{ marginBottom: 32 }}>
          <h3>{r.label}</h3>
          <StripStack
            label={r.label}
            run={r.run}
            products={model.products}
            families={model.families}
            economics={model.economics}
            day={9}
          />
        </section>
      ))}
    </>
  );
}
