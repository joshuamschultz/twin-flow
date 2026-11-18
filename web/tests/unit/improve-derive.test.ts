import { describe, expect, it } from "vitest";
import {
  checkSummary,
  dotLayout,
  rangeWords,
} from "../../src/decision/improve/derive";

describe("dotLayout", () => {
  it("puts zero inside the strip and stacks dots that land together", () => {
    const out = dotLayout([-100, 50, 50, 50, 300], 400, 6);
    expect(out.zeroX).toBeGreaterThan(0);
    expect(out.zeroX).toBeLessThan(400);
    const same = out.dots.filter((d) => d.gap === 50);
    expect(new Set(same.map((d) => d.y)).size).toBe(3);
    expect(out.dots.find((d) => d.gap === -100)!.x).toBeLessThan(out.zeroX);
    expect(out.dots.find((d) => d.gap === 300)!.x).toBeGreaterThan(out.zeroX);
  });

  it("copes with no gaps", () => {
    expect(dotLayout([], 400, 6).dots).toEqual([]);
  });
});

describe("rangeWords", () => {
  it("says the likely range in money", () => {
    expect(rangeWords(1400, 5000)).toBe("$1,400 to $5,000");
    expect(rangeWords(-900, 4100)).toBe("−$900 to $4,100");
    expect(rangeWords(null, null)).toBe("not known yet");
  });
});

describe("checkSummary", () => {
  it("counts passes", () => {
    expect(
      checkSummary([{ pass: true }, { pass: false }, { pass: true }]),
    ).toBe("2 of 3 checks pass");
  });
});
