import { describe, expect, it } from "vitest";
import { checkSummary, rangeWords } from "../../src/decision/improve/derive";

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
