import "./details.css";
import { DownloadJson } from "./DownloadJson";
import { EarlyLeaderNote } from "./EarlyLeaderNote";
import { FullTable } from "./FullTable";
import { InfoWorth } from "./InfoWorth";
import { MethodNote } from "./MethodNote";
import { Card, Section } from "./primitives";
import { ProblemList } from "./ProblemList";
import { RunFacts } from "./RunFacts";
import type { RaceViewModel } from "./types";

/** The Details section: run facts, the full table, the method, problems and the download.
 * Always last (DESIGN.md 3.11). */
export function Details({ model }: { model: RaceViewModel }) {
  return (
    <Section
      id="details"
      eyebrow="Details"
      question="The full numbers"
      actions={<DownloadJson model={model} />}
    >
      <div className="tf-det-stack">
        <RunFacts model={model} />
        <FullTable model={model} />
        <InfoWorth model={model} />
        <Card>
          <MethodNote model={model} />
          <EarlyLeaderNote model={model} />
        </Card>
        <ProblemList
          problems={model.problems}
          warnings={model.warnings}
          policies={model.policies}
        />
      </div>
    </Section>
  );
}
