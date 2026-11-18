// Compare two and What if, alone (B-15 to B-17).
import { CompareTable } from "../decision/CompareTable";
import type { RaceViewModel } from "../decision/types";
import { WhatIf } from "../decision/WhatIfPanel";

export function CompareFixture({ model }: { model: RaceViewModel }) {
  return (
    <div data-fixture="compare">
      <CompareTable
        policies={model.policies}
        runs={model.about.episodes}
        horizonDays={model.about.horizon_days}
      />
    </div>
  );
}

export function WhatIfFixture({ model }: { model: RaceViewModel }) {
  return (
    <div data-fixture="whatif">
      <WhatIf
        policies={model.policies}
        config={model.what_if}
        runs={model.about.episodes}
      />
    </div>
  );
}
