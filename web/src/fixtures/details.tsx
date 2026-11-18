// Tuning and Details parts, alone (B-23, B-24). Names: tuning-curve (takes &policy=<id>),
// tuning-table, tuning, details, full-table, problems, download.
import type { ReactNode } from "react";
import { Details } from "../decision/Details";
import { DownloadJson } from "../decision/DownloadJson";
import { FullTable } from "../decision/FullTable";
import { ProblemList } from "../decision/ProblemList";
import { TuningCurve } from "../decision/TuningCurve";
import { TuningTable } from "../decision/TuningTable";
import { TuningView } from "../decision/TuningView";
import { tuningRows } from "../decision/tuningModel";
import type { RaceViewModel } from "../decision/types";

type Part = (p: { model: RaceViewModel }) => ReactNode;

function TuningCurveFixture({ model }: { model: RaceViewModel }) {
  const policy =
    new URLSearchParams(location.search).get("policy") ??
    model.recommendation.policy;
  const row = model.tuning.find((t) => t.policy === policy)!;
  return <TuningCurve tuning={row} label={policy ?? ""} />;
}

export const DETAILS_FIXTURES: Record<string, Part> = {
  "tuning-curve": TuningCurveFixture,
  "tuning-table": ({ model }) => <TuningTable rows={tuningRows(model)} />,
  tuning: ({ model }) => <TuningView model={model} />,
  details: ({ model }) => <Details model={model} />,
  "full-table": ({ model }) => <FullTable model={model} />,
  problems: ({ model }) => (
    <ProblemList
      problems={model.problems}
      warnings={model.warnings}
      policies={model.policies}
    />
  ),
  download: ({ model }) => <DownloadJson model={model} />,
};
