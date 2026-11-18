import { describe, expect, it } from "vitest";
import {
  MINUS,
  joinWords,
  num,
  pct,
  round100,
  signedNum,
  signedUsd,
  usd,
  usdCompact,
} from "../../src/decision/format";

describe("money", () => {
  it("formats whole dollars with thousands separators", () => {
    expect(usd(22088)).toBe("$22,088");
    expect(usd(22088.4)).toBe("$22,088");
    expect(usd(0)).toBe("$0");
  });
  it("uses a real minus sign, never a hyphen", () => {
    expect(usd(-23545)).toBe(`${MINUS}$23,545`);
    expect(MINUS).toBe("−");
    expect(signedUsd(-6153)).toBe("−$6,153");
  });
  it("signs gains with a plus", () => {
    expect(signedUsd(22088)).toBe("+$22,088");
    expect(signedUsd(0)).toBe("$0");
  });
  it("rounds half away from zero, including negatives", () => {
    expect(usd(0.5)).toBe("$1");
    expect(usd(-0.5)).toBe(`${MINUS}$1`);
    expect(usd(-0.4)).toBe("$0");
  });
  it("rounds to the nearest $100 for ranges and per-year figures", () => {
    expect(round100(134370)).toBe(134400);
    expect(round100(19733)).toBe(19700);
    expect(round100(-23545)).toBe(-23500);
  });
  it("writes compact axis labels", () => {
    expect(usdCompact(20000, true)).toBe("+$20k");
    expect(usdCompact(-10000, true)).toBe(`${MINUS}$10k`);
    expect(usdCompact(0, true)).toBe("$0");
    expect(usdCompact(2500)).toBe("$2.5k");
  });
});

describe("numbers", () => {
  it("formats counts and signed counts", () => {
    expect(num(1312.7)).toBe("1,313");
    expect(num(45.25, 1)).toBe("45.3");
    expect(signedNum(-13.7, 1)).toBe(`${MINUS}13.7`);
    expect(signedNum(2.7, 1)).toBe("+2.7");
  });
  it("formats shares as percent", () => {
    expect(pct(0.9673)).toBe("96.7%");
    expect(pct(0.085, 1)).toBe("8.5%");
  });
  it("joins words like people do", () => {
    expect(joinWords(["a"])).toBe("a");
    expect(joinWords(["a", "b"])).toBe("a and b");
    expect(joinWords(["a", "b", "c"])).toBe("a, b and c");
  });
});
