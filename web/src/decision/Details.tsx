import "./details.css";
import { DownloadJson } from "./DownloadJson";
import { EarlyLeaderNote } from "./EarlyLeaderNote";
import { FullTable } from "./FullTable";
import { InfoWorth } from "./InfoWorth";
import { MethodNote } from "./MethodNote";
import { ProblemList } from "./ProblemList";
import { RunFacts } from "./RunFacts";
import type { RaceViewModel } from "./types";

/** The Details section: run facts, the full table, the method, problems and the download.
 * Always last (DESIGN.md 3.11). */
export function Details({ model }: { model: RaceViewModel }) {
  return (
    <section className="tf-sec" id="details" aria-labelledby="details-h">
      <div className="tf-sec-head tf-sec-row">
        <div>
          <div className="tf-eyebrow">Details</div>
          <h2 id="details-h">The full numbers</h2>
        </div>
        <DownloadJson model={model} />
      </div>
      <div className="tf-det-stack">
        <RunFacts model={model} />
        <FullTable model={model} />
        <InfoWorth model={model} />
        <div className="tf-card">
          <MethodNote model={model} />
          <EarlyLeaderNote model={model} />
        </div>
        <ProblemList
          problems={model.problems}
          warnings={model.warnings}
          policies={model.policies}
        />
      </div>
    </section>
  );
}
