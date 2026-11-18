import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Details } from "../../src/decision/Details";
import { DownloadJson } from "../../src/decision/DownloadJson";
import { EarlyLeaderNote } from "../../src/decision/EarlyLeaderNote";
import { FullTable } from "../../src/decision/FullTable";
import { InfoWorth } from "../../src/decision/InfoWorth";
import { MethodNote } from "../../src/decision/MethodNote";
import { ProblemList } from "../../src/decision/ProblemList";
import { RunFacts } from "../../src/decision/RunFacts";
import type { RaceViewModel } from "../../src/decision/types";

const full = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;

const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/\s+/g, " ");
const rowsOf = (html: string) =>
  [...html.matchAll(/<tr[^>]*>(.*?)<\/tr>/gs)].map((m) => text(m[1]).trim());

describe("FullTable", () => {
  it("has one row per rule: 12 rows in the golden race", () => {
    const html = renderToStaticMarkup(<FullTable model={full} />);
    expect(html.match(/<tbody>.*<\/tbody>/s)![0].match(/<tr/g)).toHaveLength(
      12,
    );
  });
  it("labels the range with the interval and holds the columns the design lists", () => {
    const html = renderToStaticMarkup(<FullTable model={full} />);
    const head = text(html.match(/<thead>.*<\/thead>/s)![0]);
    for (const col of [
      "Rule",
      "Profit per run",
      "95% range",
      "vs today",
      "Runs won",
      "Filled",
      "Changeovers",
      "Colour changes",
      "Stock units",
      "Source",
    ]) {
      expect(head).toContain(col);
    }
  });
  it("writes the pick's numbers the way the mockup does", () => {
    const html = renderToStaticMarkup(<FullTable model={full} />);
    const first = rowsOf(html).find((r) => r.includes("Family campaign"))!;
    expect(first).toContain("$65,410");
    expect(first).toContain("$64,252 to $66,568");
    expect(first).toContain("+$22,088");
    expect(first).toContain("40 of 40");
    expect(first).toContain("96.7%");
    expect(first).toContain("45.3");
    expect(first).toContain("8.4");
    expect(first).toContain("3,378");
  });
  it("marks today's rule instead of a gap and uses the real minus sign", () => {
    const html = renderToStaticMarkup(<FullTable model={full} />);
    const today = rowsOf(html).find((r) => r.includes("Lowest cover"))!;
    expect(today).toContain("today");
    expect(today).not.toContain("of 40");
    expect(
      rowsOf(html).find((r) =>
        r.includes(full.policies.find((p) => p.policy === "idle")!.label),
      )!,
    ).toContain("−$78,641");
  });
  it("takes the range label from the interval", () => {
    const m = { ...full, about: { ...full.about, interval: 0.9 } };
    expect(text(renderToStaticMarkup(<FullTable model={m} />))).toContain(
      "90% range",
    );
  });
  it("scrolls sideways inside its card and can be reached by keyboard", () => {
    const html = renderToStaticMarkup(<FullTable model={full} />);
    expect(html).toContain('tabindex="0"');
    expect(html).toContain("tf-scroll");
  });
});

describe("RunFacts", () => {
  it("shows runs, days, seed and today's rule", () => {
    const t = text(renderToStaticMarkup(<RunFacts model={full} />));
    expect(t).toContain("Runs per rule 40");
    expect(t).toContain("Days per run 60");
    expect(t).toContain("Random seed 0");
    expect(t).toContain("Today's rule Lowest cover");
  });
});

describe("MethodNote", () => {
  it("quotes the method and explains the numbers in plain words", () => {
    const t = text(renderToStaticMarkup(<MethodNote model={full} />));
    expect(t).toContain(full.about.method);
    expect(t).toContain('"vs today" is the average gap');
    expect(t).toContain(full.delta.assumption);
    expect(t).not.toContain("—");
  });
});

describe("ProblemList", () => {
  it("says so when nothing broke", () => {
    const t = text(
      renderToStaticMarkup(<ProblemList problems={[]} warnings={[]} />),
    );
    expect(t).toContain("No problems were found in this race.");
  });
  it("lists file, line, message and fix for a broken rule", () => {
    const html = renderToStaticMarkup(
      <ProblemList
        warnings={[]}
        problems={[
          {
            policy: "my_rule",
            file: "policies/my_rule.py",
            line: 12,
            message: "name 'stock' is not defined",
            fix: "Use state.stock instead of stock.",
          },
          { message: "No file for this one", fix: "Add the file." },
        ]}
      />,
    );
    const t = text(html);
    expect(t).toContain("policies/my_rule.py:12");
    expect(t).toContain("name 'stock' is not defined");
    expect(t).toContain("Fix: Use state.stock instead of stock.");
    expect(t).toContain("My rule");
    expect(t).toContain("No file for this one");
    expect(t).toContain("2 rules could not run");
    expect(html.match(/<li/g)).toHaveLength(2);
  });
  it("lists warnings with their fix", () => {
    const t = text(
      renderToStaticMarkup(
        <ProblemList
          problems={[]}
          warnings={[
            {
              kind: "dollars",
              text: "Dollars are missing.",
              fix: "Set a price.",
            },
          ]}
        />,
      ),
    );
    expect(t).toContain("Dollars are missing.");
    expect(t).toContain("Fix: Set a price.");
  });
});

describe("DownloadJson", () => {
  it("is a data: link that holds the whole model", () => {
    const html = renderToStaticMarkup(<DownloadJson model={full} />);
    const href = html
      .match(/href="([^"]+)"/)![1]
      .replaceAll("&amp;", "&")
      .replaceAll("&#x27;", "'");
    expect(href.startsWith("data:application/json;charset=utf-8,")).toBe(true);
    expect(html).toContain('download="race.json"');
    expect(text(html)).toContain("Download race.json");
    const body = decodeURIComponent(href.slice(href.indexOf(",") + 1));
    expect(JSON.parse(body)).toEqual(full);
  });
});

describe("InfoWorth", () => {
  it("is one table of rules by view, with the money for each", () => {
    const html = renderToStaticMarkup(<InfoWorth model={full} />);
    expect(text(html)).toContain("What is information worth?");
    expect(text(html)).toContain("Every rule saw the full view.");
    const r = rowsOf(html).find((x) => x.includes("Family campaign"))!;
    expect(r).toContain("$65,410");
  });
  it("shows the drop from the first view when there are several", () => {
    const m = structuredClone(full);
    m.views.observations = ["full", "stock only"];
    for (const [, row] of Object.entries(m.views.dollars))
      row["stock only"] = row.full - 1000;
    const html = renderToStaticMarkup(<InfoWorth model={m} />);
    expect(text(html)).toContain(
      "The drop from the first column is what that information is worth.",
    );
    expect(rowsOf(html).find((x) => x.includes("Family campaign"))!).toContain(
      "−$1,000",
    );
  });
});

describe("EarlyLeaderNote", () => {
  it("writes the model's sentence", () => {
    const t = text(renderToStaticMarkup(<EarlyLeaderNote model={full} />));
    expect(t).toContain(full.early_leader!.words);
  });
  it("shows nothing when there is no early leader", () => {
    expect(
      renderToStaticMarkup(
        <EarlyLeaderNote model={{ ...full, early_leader: null }} />,
      ),
    ).toBe("");
  });
});

describe("Details", () => {
  it("is the last section, with the title, facts, table, notes and the problems list", () => {
    const html = renderToStaticMarkup(<Details model={full} />);
    expect(html).toContain('id="details"');
    const t = text(html);
    for (const part of [
      "The full numbers",
      "Runs per rule",
      "95% range",
      "What is information worth?",
      "How to read this",
      "No problems were found in this race.",
      "Download race.json",
    ]) {
      expect(t).toContain(part);
    }
    expect(html).not.toContain("—");
  });
});
