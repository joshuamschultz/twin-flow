import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import type { RaceViewModel } from "../../src/decision/types";
import {
  dayFacts,
  keyDay,
  nextPlayDay,
  parseDay,
  replayRuns,
  replayTakeaway,
} from "../../src/decision/replay";

const FULL = fileURLToPath(
  new URL("../../../docs/design/race-view-model.full.json", import.meta.url),
);
const model = JSON.parse(readFileSync(FULL, "utf8")) as RaceViewModel;
const pick = model.example_runs.family_campaign;

describe("replay day facts (B-19)", () => {
  it("tells day 9 of the pick in words", () => {
    const f = dayFacts(pick, 9, model.products, model.economics)!;
    expect(f.makes).toBe("BLK-M");
    expect(f.change).toBe(
      "Colour change to black ($1,200, half the day lost).",
    );
    expect(f.outLabel).toBe("7 of 12");
    expect(f.profitSoFar).toBe(4385);
    expect(f.sentence).toBe(
      "Day 9. Colour change to black ($1,200, half the day lost). Out of stock: " +
        "BLK-S, BLK-L, BLK-XL, NAT-S, NAT-M, NAT-L, NAT-XL. Profit today: −$226.",
    );
    expect(f.sentence).not.toMatch(/—/);
  });

  it("counts changeovers so far, both kinds", () => {
    const f = dayFacts(pick, 9, model.products, model.economics)!;
    const want = pick.days
      .slice(0, 9)
      .filter((d) => d.changeover || d.cross_family).length;
    expect(f.changeoversSoFar).toBe(want);
  });

  it("writes the takeaway with the golden numbers", () => {
    expect(replayTakeaway(replayRuns(model), model.products)).toBe(
      "Same orders, two very different floors. In this run, today's rule changes colour 41 " +
        "times. The pick changes colour 9 times and runs each colour for about a week. " +
        "Product-days out of stock: 124 today, 53 with the pick.",
    );
  });
});

describe("replay controls (B-19)", () => {
  it("steps by key: arrows 1, Page Up and Down 10, Home and End", () => {
    expect(keyDay("ArrowRight", 9, 60)).toBe(10);
    expect(keyDay("ArrowUp", 9, 60)).toBe(10);
    expect(keyDay("ArrowLeft", 9, 60)).toBe(8);
    expect(keyDay("ArrowDown", 1, 60)).toBe(1);
    expect(keyDay("PageUp", 9, 60)).toBe(19);
    expect(keyDay("PageUp", 55, 60)).toBe(60);
    expect(keyDay("PageDown", 9, 60)).toBe(1);
    expect(keyDay("Home", 33, 60)).toBe(1);
    expect(keyDay("End", 33, 60)).toBe(60);
    expect(keyDay("a", 33, 60)).toBeNull();
  });

  it("plays forward and stops at the last day", () => {
    expect(nextPlayDay(59, 60)).toBe(60);
    expect(nextPlayDay(60, 60)).toBeNull();
  });

  it("reads the day from the URL value, clamped to the run", () => {
    expect(parseDay("9", 60)).toBe(9);
    expect(parseDay("99", 60)).toBe(60);
    expect(parseDay("0", 60)).toBe(1);
    expect(parseDay("x", 60)).toBe(1);
    expect(parseDay("", 60)).toBe(1);
  });
});
