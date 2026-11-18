import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  MEASURES,
  compareRows,
  compareSentence,
  runsWon,
  toneOf,
} from "../../src/decision/compare";
import type { PolicyRow, RaceViewModel } from "../../src/decision/types";

const model = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;
const rule = (id: string): PolicyRow =>
  model.policies.find((p) => p.policy === id)!;

describe("MEASURES", () => {
  it("fixes the better direction per measure in one place", () => {
    const dir = Object.fromEntries(MEASURES.map((m) => [m.id, m.better]));
    expect(dir.profit).toBe("up");
    expect(dir.fill_rate).toBe("up");
    expect(dir.changeovers).toBe("down");
    expect(dir.cross_family).toBe("down");
    expect(dir.avg_stock).toBe("none");
    expect(dir.rest_days).toBe("none");
    expect(dir.revenue).toBe("up");
    expect(dir.setup).toBe("down");
    expect(dir.holding).toBe("down");
    expect(dir.lost_sales).toBe("down");
  });

  it("groups rows as Result, On the floor, Where the money goes", () => {
    const groups = [...new Set(MEASURES.map((m) => m.group))];
    expect(groups).toEqual([
      "Result",
      "On the floor",
      "Where the money goes, per run",
    ]);
  });
});

describe("toneOf", () => {
  it("is blue when A is better, orange when worse, grey when neutral", () => {
    expect(toneOf(5, "up")).toBe("g");
    expect(toneOf(-5, "up")).toBe("l");
    expect(toneOf(-5, "down")).toBe("g");
    expect(toneOf(5, "down")).toBe("l");
    expect(toneOf(5, "none")).toBe("n");
    expect(toneOf(0, "up")).toBe("n");
  });
});

describe("compare two rules", () => {
  const a = rule("s_big_s");
  const b = rule("lowest_cover");

  it("counts runs won on the same orders", () => {
    expect(runsWon(a, b)).toBe(36);
    expect(runsWon(rule("family_campaign"), b)).toBe(40);
  });

  it("says Reorder point vs Lowest cover in one sentence", () => {
    const text = compareSentence(a, b, 40);
    expect(text).toBe(
      "Reorder point makes $13,691 more per run than Lowest cover. It won 36 of 40 runs on the same orders.",
    );
  });

  it("says less when A is worse", () => {
    expect(compareSentence(b, a, 40)).toMatch(
      /^Lowest cover makes \$13,691 less per run than Reorder point\. It won 4 of 40 runs/,
    );
  });

  it("asks for two different rules when A equals B", () => {
    expect(compareSentence(a, a, 40)).toBe("Pick two different rules.");
  });

  it("builds rows with values, a difference and a pill tone", () => {
    const rows = compareRows(a, b, 60);
    const profit = rows.find((r) => r.id === "profit")!;
    expect(profit.a).toBe(57013);
    expect(profit.b).toBe(43322);
    expect(profit.tone).toBe("g");
    expect(profit.deltaText).toBe("+$13,691");
    const setup = rows.find((r) => r.id === "setup")!;
    expect(setup.tone).toBe("g"); // A spends less on changeovers
    const holding = rows.find((r) => r.id === "holding")!;
    expect(holding.tone).toBe("l"); // A spends more on keeping stock
    const stock = rows.find((r) => r.id === "avg_stock")!;
    expect(stock.tone).toBe("n");
    const rest = rows.find((r) => r.id === "rest_days")!;
    expect(rest.aText).toBe("9.5"); // 0.1579 x 60 days
    const fill = rows.find((r) => r.id === "fill_rate")!;
    expect(fill.aText).toBe("94.4%");
    expect(fill.deltaText).toBe("+0.4 pts");
  });

  it("drops the late row when both rules have no late cost", () => {
    expect(compareRows(a, b, 60).some((r) => r.id === "late")).toBe(false);
  });

  it("says same for equal values", () => {
    const rows = compareRows(
      rule("lowest_cover"),
      rule("lowest_stock_first"),
      60,
    );
    expect(rows.every((r) => r.deltaText === "same")).toBe(true);
  });

  it("uses no em dashes", () => {
    const text =
      JSON.stringify(compareRows(a, b, 60)) + compareSentence(a, b, 40);
    expect(text).not.toContain(String.fromCharCode(0x2014));
  });
});
