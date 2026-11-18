import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TuningCurve } from "../../src/decision/TuningCurve";
import { TuningTable } from "../../src/decision/TuningTable";
import { TuningView } from "../../src/decision/TuningView";
import { tuningRows } from "../../src/decision/tuningModel";
import type { RaceViewModel } from "../../src/decision/types";

const full = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;

const text = (html: string) =>
  html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

describe("TuningCurve", () => {
  it("is a chart frame named for the chosen setting, with a table toggle", () => {
    const t = full.tuning.find((x) => x.policy === "family_campaign")!;
    const html = renderToStaticMarkup(
      <TuningCurve tuning={t} label="Family campaign" />,
    );
    expect(html).toContain('role="group"');
    expect(html).toContain("Chosen: target days 16, $67,186.");
    expect(html).toContain("Show as table");
  });
  it("shows a dashed box when no setting gave a result", () => {
    const html = renderToStaticMarkup(
      <TuningCurve
        label="X"
        tuning={{
          policy: "x",
          chosen: null,
          tried: 1,
          trials: [
            {
              settings: { a: 1 },
              score: null,
              dollars: null,
              problem: "broke",
              chosen: false,
            },
          ],
        }}
      />,
    );
    expect(html).toContain("tuning-empty");
  });
});

describe("TuningTable", () => {
  it("shows a touchy pill only where the model says touchy", () => {
    const rows = tuningRows(full);
    const html = renderToStaticMarkup(<TuningTable rows={rows} />);
    const touchy = rows.filter((r) => r.touchy).length;
    expect(touchy).toBeGreaterThan(0);
    expect(html.match(/tf-l/g)).toHaveLength(touchy);
    expect(html.match(/tf-n/g)).toHaveLength(rows.length - touchy);
    expect(html.match(/<tr/g)).toHaveLength(rows.length + 1);
  });
});

describe("TuningView", () => {
  it("leads with the question, shows the pick's sentence and has no em dash", () => {
    const html = renderToStaticMarkup(<TuningView model={full} />);
    expect(html).toContain("Did each rule get a fair try?");
    const pick = full.tuning.find(
      (t) => t.policy === full.recommendation.policy,
    )!;
    expect(text(html)).toContain(pick.words);
    expect(html).not.toContain("—");
  });
  it("says so when nothing was tuned", () => {
    const html = renderToStaticMarkup(
      <TuningView model={{ ...full, tuning: [] }} />,
    );
    expect(text(html)).toContain("nothing was tuned");
  });
});
