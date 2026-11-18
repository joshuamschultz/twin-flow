import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  fmtMoney,
  fmtPct,
  settingName,
  settingText,
  settingValue,
} from "../../src/decision/detailsFormat";
import { curveSpec, tuningRows } from "../../src/decision/tuningModel";
import type { RaceViewModel, Tuning } from "../../src/decision/types";

const full = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;

const trial = (
  settings: Record<string, number>,
  dollars: number | null,
  chosen = false,
) => ({
  settings,
  score: dollars,
  dollars,
  problem: dollars === null ? "broke" : null,
  chosen,
});

describe("formatters", () => {
  it("writes money with a real minus and thousands separators", () => {
    expect(fmtMoney(22088)).toBe("$22,088");
    expect(fmtMoney(22088, { signed: true })).toBe("+$22,088");
    expect(fmtMoney(-1500, { signed: true })).toBe("−$1,500");
    expect(fmtMoney(-1500)).toBe("−$1,500");
    expect(fmtMoney(0, { signed: true })).toBe("$0");
  });
  it("writes shares as percents", () => {
    expect(fmtPct(0.9673)).toBe("96.7%");
    expect(fmtPct(null)).toBe("–");
  });
  it("turns setting names and values into words", () => {
    expect(settingName("target_days")).toBe("target days");
    expect(settingValue(10)).toBe("10");
    expect(settingValue(0.5)).toBe("0.5");
    expect(settingValue(true)).toBe("yes");
    expect(settingText({ target_days: 14, trigger_share: 0.7 })).toBe(
      "target days 14, trigger share 0.7",
    );
  });
});

describe("curveSpec", () => {
  it("draws one line for a one-setting rule and marks the chosen point", () => {
    const row = full.tuning.find((t) => t.policy === "family_campaign")!;
    const spec = curveSpec(row);
    expect(spec.xName).toBe("target days");
    expect(spec.panels).toHaveLength(1);
    expect(spec.panels[0].title).toBeNull();
    expect(spec.panels[0].points.map((p) => p.label)).toEqual([
      "6",
      "8",
      "12",
      "16",
      "20",
    ]);
    expect(
      spec.panels[0].points.filter((p) => p.chosen).map((p) => p.label),
    ).toEqual(["16"]);
    expect(spec.yMin).toBeLessThan(60707);
    expect(spec.yMax).toBeGreaterThan(67186);
    expect(spec.ticks.length).toBeGreaterThanOrEqual(3);
  });
  it("draws small multiples for a two-setting rule, one panel per second value", () => {
    const row = full.tuning.find((t) => t.policy === "setup_aware_runout")!;
    const spec = curveSpec(row);
    const keys = Object.keys(row.trials[0].settings);
    expect(keys).toHaveLength(2);
    expect(spec.panels.length).toBeGreaterThan(1);
    expect(spec.groupName).not.toBeNull();
    const total = spec.panels.reduce((n, p) => n + p.points.length, 0);
    expect(total).toBe(row.trials.length);
    expect(
      spec.panels.flatMap((p) => p.points).filter((p) => p.chosen),
    ).toHaveLength(1);
    for (const p of spec.panels) expect(p.title).toMatch(/^.+ .+$/);
  });
  it("keeps trials that did not run out of the line and lists them apart", () => {
    const t: Tuning = {
      policy: "x",
      chosen: { a: 2 },
      tried: 3,
      trials: [
        trial({ a: 1 }, 100),
        trial({ a: 2 }, 200, true),
        trial({ a: 3 }, null),
      ],
    };
    const spec = curveSpec(t);
    expect(spec.panels[0].points.map((p) => p.label)).toEqual(["1", "2"]);
    expect(spec.skipped.map((p) => p.label)).toEqual(["3"]);
    expect(spec.skipped[0].problem).toBe("broke");
  });
  it("copes with a rule that has no trials", () => {
    const spec = curveSpec({ policy: "x", chosen: null, tried: 0, trials: [] });
    expect(spec.panels).toEqual([]);
  });
});

describe("tuningRows", () => {
  it("has one row per tuned rule, the pick first, then by rank", () => {
    const rows = tuningRows(full);
    expect(rows).toHaveLength(full.tuning.length);
    expect(rows[0].policy).toBe(full.recommendation.policy);
    expect(rows[0].isPick).toBe(true);
    expect(rows.filter((r) => r.isPick)).toHaveLength(1);
  });
  it("says steady or touchy from the model and never recomputes it", () => {
    const rows = tuningRows(full);
    for (const row of rows) {
      const src = full.tuning.find((t) => t.policy === row.policy)!;
      expect(row.touchy).toBe(!!src.touchy);
      expect(row.pill).toBe(src.touchy ? "touchy" : "steady");
      expect(row.spread).toBe(src.spread ?? 0);
      expect(row.tried).toBe(src.tried);
    }
    expect(rows.some((r) => r.pill === "touchy")).toBe(true);
  });
  it("names the best setting in words and uses rule labels", () => {
    const row = tuningRows(full).find((r) => r.policy === "family_campaign")!;
    expect(row.label).toBe("Family campaign");
    expect(row.best).toBe("target days 16");
  });
});
