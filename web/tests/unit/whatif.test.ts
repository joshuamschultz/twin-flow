import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  flipPoints,
  rerank,
  scoreAt,
  type Multipliers,
} from "../../src/decision/whatif";
import type { RaceViewModel } from "../../src/decision/types";

const model = JSON.parse(
  readFileSync(
    new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
    "utf8",
  ),
) as RaceViewModel;
const policies = model.policies;
const config = model.what_if;
const ONE: Multipliers = { setup: 1, holding: 1, lost_sales: 1 };

describe("scoreAt", () => {
  it("re-prices the stored cost parts", () => {
    const fc = policies.find((p) => p.policy === "family_campaign")!;
    expect(scoreAt(fc, ONE)).toBe(65410);
    expect(scoreAt(fc, { ...ONE, holding: 3 })).toBe(65410 - 2 * 23080);
  });
});

describe("rerank", () => {
  it("at 1, 1, 1 equals the real ranking", () => {
    const real = [...policies]
      .sort((a, b) => a.rank - b.rank)
      .map((p) => p.policy);
    const rows = rerank(policies, ONE);
    expect(rows.map((r) => r.policy)).toEqual(real);
    expect(rows.every((r) => r.move === 0)).toBe(true);
  });

  it("holding at 3 times puts Setup-aware run-out first by $1,046", () => {
    const rows = rerank(policies, { ...ONE, holding: 3 });
    expect(rows[0].policy).toBe("setup_aware_runout");
    expect(rows[0].score - rows[1].score).toBe(1046);
    expect(rows[0].move).toBe(2); // up from third
  });

  it("gives each row its move from the real ranking", () => {
    const rows = rerank(policies, { ...ONE, holding: 3 });
    const fc = rows.find((r) => r.policy === "family_campaign")!;
    expect(fc.move).toBeLessThan(0);
    expect(rows.reduce((s, r) => s + r.move, 0)).toBe(0);
  });
});

describe("flipPoints", () => {
  const flips = flipPoints(policies, "family_campaign", config);
  const step = config.step;

  it("finds the changeover flip near 0.45", () => {
    expect(flips.setup.above).toBeNull();
    expect(flips.setup.below!.leader).toBe("setup_aware_runout");
    expect(Math.abs(flips.setup.below!.at - 0.45)).toBeLessThanOrEqual(step);
  });

  it("finds the keeping stock flip near 2.45", () => {
    expect(flips.holding.below).toBeNull();
    expect(flips.holding.above!.leader).toBe("setup_aware_runout");
    expect(Math.abs(flips.holding.above!.at - 2.45)).toBeLessThanOrEqual(step);
  });

  it("finds no flip for lost sales", () => {
    expect(flips.lost_sales.below).toBeNull();
    expect(flips.lost_sales.above).toBeNull();
  });

  it("returns exact slider-friendly values, not float noise", () => {
    expect(flips.setup.below!.at).toBe(0.45);
    expect(flips.holding.above!.at).toBe(2.45);
  });

  it("stops safely on a zero step or no rules", () => {
    const zero = flipPoints(policies, "family_campaign", {
      ...config,
      step: 0,
    });
    expect(zero.setup).toEqual({ below: null, above: null });
    expect(flipPoints([], "x", config).holding).toEqual({
      below: null,
      above: null,
    });
  });
});
