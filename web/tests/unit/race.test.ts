import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  axisLimit,
  beatToday,
  cautionLine,
  dotLayout,
  dotsCaption,
  familyColor,
  floorValues,
  gapSeries,
  hero,
  labelEnds,
  leaderboard,
  numberWord,
  waterfall,
} from "../../src/decision/race";
import type { RaceViewModel } from "../../src/decision/types";

const golden = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;
const clone = () => JSON.parse(JSON.stringify(golden)) as RaceViewModel;

describe("hero words", () => {
  it("says switch, with the gain and the runs won", () => {
    const h = hero(golden);
    expect(h.kind).toBe("switch");
    expect([h.before, h.name, h.after].join("")).toBe(
      "Switch to Family campaign.",
    );
    expect(h.perRun).toBe("+$22,088");
    expect(h.perYear).toBe("+$134,400");
    expect(h.range).toBe(
      "The gain is likely between $19,700 and $24,400 per run.",
    );
    expect(h.badge).toBe("Very likely better");
    expect(h.lede.map((p) => p.text).join("")).toBe(
      "It makes $22,088 more in each 60-day run than what you do today (Lowest cover). " +
        "It won 40 of 40 test runs on the same customer orders.",
    );
  });
  it("leads with the yearly figure from the 180-day check (owner decision)", () => {
    const h = hero(golden);
    expect(h.year).toEqual({
      value: "+$109,000",
      label: "more profit per year, from a 180-day check",
      checked: true,
    });
    expect(h.quickYear).toBe(
      "Quick math from the 60-day runs says +$134,400 a year.",
    );
  });
  it("falls back to the 60-day yearly guess, with its condition, when there is no long check", () => {
    const m = clone();
    m.delta!.long_check = null;
    const h = hero(m);
    expect(h.year).toEqual({
      value: "+$134,400",
      label: "more per year, if every 60 days looks like these runs",
      checked: false,
    });
    expect(h.quickYear).toBeNull();
  });
  it("keeps today's rule when it is the pick", () => {
    const m = clone();
    m.recommendation.same_as_baseline = true;
    m.confidence!.level = "same_policy";
    const h = hero(m);
    expect([h.before, h.name, h.after].join("")).toBe(
      "Keep Family campaign. Nothing tested beats it.",
    );
    expect(h.kind).toBe("keep");
  });
  it("says test more when the lead is not proven", () => {
    const m = clone();
    m.confidence!.level = "maybe_better";
    expect([hero(m).before, hero(m).name, hero(m).after].join("")).toBe(
      "Family campaign may be better. Test more before you switch.",
    );
  });
  it("has no gain without a baseline", () => {
    const m = clone();
    m.baseline = null;
    m.delta = null;
    const h = hero(m);
    expect([h.before, h.name, h.after].join("")).toBe(
      "Family campaign made the most money.",
    );
    expect(h.perRun).toBeNull();
    expect(h.note).toContain("experiment.race.baseline");
  });
  it("says so when only one rule ran", () => {
    const m = clone();
    m.policies = m.policies.slice(0, 1);
    expect(hero(m).note).toBe(
      "Only one rule ran. Add a second rule to compare.",
    );
  });
  it("notes a score that is not dollars", () => {
    const m = clone();
    m.about.score_is_dollars = false;
    m.about.score = "fill";
    expect(hero(m).note).toBe("Ranked by fill. Dollars shown for scale.");
  });
});

describe("run dots", () => {
  it("places 40 dots with no overlap", () => {
    const gaps = golden.confidence!.gaps!;
    const { dots } = dotLayout(gaps, 600, { r: 3.6, pad: 8 });
    expect(dots).toHaveLength(40);
    for (let i = 0; i < dots.length; i++)
      for (let j = i + 1; j < dots.length; j++) {
        const d = Math.hypot(dots[i].x - dots[j].x, dots[i].y - dots[j].y);
        expect(d).toBeGreaterThanOrEqual(7.2 - 1e-6);
      }
  });
  it("captions all wins and some losses", () => {
    expect(dotsCaption(golden.confidence!.gaps!)).toBe(
      "Each dot is one test run. All 40 dots are right of zero, so the new rule won every time.",
    );
    const gaps = [...golden.confidence!.gaps!];
    for (let i = 0; i < 6; i++) gaps[i] = -1000 - i;
    expect(dotsCaption(gaps)).toBe(
      "Each dot is one test run. 6 of 40 dots are left of zero. In those runs today's rule did better.",
    );
  });
});

describe("caution line", () => {
  it("uses the top caution, title and first sentence", () => {
    expect(cautionLine(golden)).toEqual({
      title: "The gain comes late.",
      text: "It is behind today's rule until day 54 of 60.",
      link: { to: "why", label: "See when it pulls ahead." },
    });
  });
});

describe("waterfall", () => {
  it("sorts gains first, names what happened, and totals delta.per_run", () => {
    const w = waterfall(golden);
    expect(w.items.map((i) => i.label)).toEqual([
      "Fewer changeovers",
      "More sales",
      "Fewer lost sales",
      "Making more product",
      "Keeping more stock",
    ]);
    expect(w.items.map((i) => i.value)).toEqual([
      35355, 5405, 1622, -7195, -13098,
    ]);
    expect(w.total).toBe(22088);
    // each bar starts where the last one ended
    let run = 0;
    for (const i of w.items) {
      expect(i.start).toBe(run);
      run += i.value;
    }
  });
});

describe("gap line", () => {
  it("draws the pick and the next two rules against today", () => {
    const s = gapSeries(golden);
    expect(s.map((x) => x.label)).toEqual([
      "Family campaign",
      "Reorder point",
      "Setup-aware run-out",
    ]);
    expect(s[0].emphasis).toBe(true);
    expect(s[0].values).toHaveLength(60);
    expect(s[0].values[41]).toBe(-23545);
  });
  it("keeps end labels apart", () => {
    const ys = labelEnds([100, 104, 98, 300], 14);
    const sorted = [...ys].sort((a, b) => a - b);
    for (let i = 1; i < sorted.length; i++)
      expect(sorted[i] - sorted[i - 1]).toBeGreaterThanOrEqual(14);
  });
});

describe("leaderboard", () => {
  it("puts today on the zero line and folds weak rules", () => {
    const lb = leaderboard(golden);
    expect(lb.main).toHaveLength(8);
    expect(lb.rest.map((r) => r.label)).toEqual([
      "Fixed wheel",
      "Best for today only",
      "Random",
      "Do nothing",
    ]);
    const today = lb.main.find((r) => r.today)!;
    expect(today.value).toBe("$43,322 now");
    expect(lb.main.find((r) => r.id === "lowest_stock_first")!.value).toBe(
      "same as today",
    );
    expect(lb.main[0].tip).toBe(
      "Family campaign: +$22,088 per run vs today. Likely range +$19,700 to +$24,400. Won 40 of 40 runs.",
    );
    expect(lb.floors).toBe(2);
    expect(axisLimit(golden.policies.filter((p) => p.rank <= 8))).toBe(26000);
  });
  it("sums up who beats today", () => {
    expect(beatToday(golden)).toBe(
      "Three rules beat today. All three switch colours less. Lowest stock first is your own file and acts the same as Lowest cover, so it scores the same.",
    );
  });
});

describe("small helpers", () => {
  it("spells small numbers", () => {
    expect(numberWord(3)).toBe("three");
    expect(numberWord(12)).toBe("12");
  });
  it("colours families named after colours with their colour", () => {
    expect(familyColor("grey", 0)).toBe("var(--tf-fam-grey)");
    expect(familyColor("natural", 2)).toBe("var(--tf-fam-natural)");
    expect(familyColor("brackets", 1)).toBe("var(--tf-fam-2)");
  });
  it("formats floor values by unit", () => {
    const [co, fill, stock, rest] = golden.floor;
    expect(floorValues(co)).toEqual(["59", "45"]);
    expect(floorValues(fill)).toEqual(["94.0%", "96.7%"]);
    expect(floorValues(stock)).toEqual(["1,313 units", "3,378"]);
    expect(floorValues(rest)).toEqual(["0", "12"]);
  });
});
